"""Main library entrypoint."""

import copy
import io
import os
import sys
import random
import math
import subprocess
import json
import yaml
import time
import six

import numpy as np
import tensorflow as tf

from google.protobuf import text_format

from opennmt import estimator as estimator_util
from opennmt import models
from opennmt.data import dataset as dataset_util
from opennmt.utils import hooks, checkpoint, misc
from opennmt.utils import evaluator


# These options require a value but we can fallback to a default one.
_CONFIG_FALLBACK = {
    "params": {},
    "train": {
        "batch_type": "examples",
        "bucket_width": 1,
        "sample_buffer_size": 500000,
        "save_summary_steps": 100
    },
    "eval": {
        "batch_size": 32,
        "eval_delay": 18000,
        "exporters": "last"
    },
    "infer": {
        "bucket_width": None,
        "batch_size": 16
    },
    "score": {
        "batch_size": 64
    }
}

class Runner(object):
  """Class for managing training, inference, and export. It is mostly a
  wrapper around ``tf.estimator.Estimator``.
  """

  def __init__(self,
               model,
               config,
               seed=None,
               num_devices=1,
               auto_config=False):
    """Initializes the runner parameters.

    Args:
      model: A :class:`opennmt.models.model.Model` instance to run.
      config: The run configuration.
      seed: The random seed to set.
      num_devices: The number of devices (GPUs) to use for training.
      auto_config: If ``True``, use automatic configuration values defined by
        :obj:`model`.

    Raises:
      NotImplementedError: If :obj:`auto_config` is ``True`` but :obj:`model`
        does not define any automatic configuration values.
    """
    self._model = model
    self._num_devices = num_devices
    self._num_replicas = num_devices
    self._seed = seed

    # Configuration priority: user config > auto config > default config.
    self._config = copy.deepcopy(_CONFIG_FALLBACK)
    if auto_config:
      model_config = self._model.auto_config(num_replicas=self._num_replicas)
      if not model_config:
        raise NotImplementedError("This model does not define any automatic configuration values")
      misc.merge_dict(self._config, model_config)
    misc.merge_dict(self._config, config)
    self._model.initialize(self._config["data"])
    tf.get_logger().info(
        "Using parameters:\n%s", yaml.dump(self._config, indent=2, default_flow_style=False))

    if seed is not None:
      np.random.seed(seed)
      random.seed(seed)
      tf.random.set_seed(seed)

  def _make_estimator(self):
    params = self._config["params"]
    train_config = self._config["train"]
    summary_steps = train_config["save_summary_steps"]

    train_distribute = None
    if self._num_devices > 1:
      devices = misc.get_devices(num_devices=self._num_devices, session_config=self._session_config)
      train_distribute = tf.distribute.MirroredStrategy(devices=devices)
    run_config = tf.estimator.RunConfig(
        model_dir=self._config["model_dir"],
        tf_random_seed=self._seed,
        save_summary_steps=summary_steps,
        session_config=self._session_config,
        log_step_count_steps=params.get("gradients_accum", 1) * summary_steps,
        train_distribute=train_distribute)
    if "save_checkpoints_steps" in train_config or "save_checkpoints_secs" in train_config:
      run_config = run_config.replace(
          save_checkpoints_secs=train_config.get("save_checkpoints_secs"),
          save_checkpoints_steps=train_config.get("save_checkpoints_steps"))
    if not self.is_chief():
      run_config = run_config.replace(
          save_checkpoints_secs=None,
          save_checkpoints_steps=None)
    if "keep_checkpoint_max" in train_config:
      run_config = run_config.replace(
          keep_checkpoint_max=train_config["keep_checkpoint_max"])

    params.setdefault("num_hypotheses", self._config["infer"].get("n_best", 1))

    return tf.estimator.Estimator(
        estimator_util.make_model_fn(
            self._model,
            eval_prediction_hooks_fn=self._make_eval_prediction_hooks_fn()),
        config=run_config,
        params=params)

  def is_chief(self):
    """Returns ``True`` if this runner is the master runner."""
    cluster_spec = os.getenv("TF_CONFIG")
    if cluster_spec is None:
      return True
    cluster_spec = json.loads(cluster_spec)
    return cluster_spec["task"]["type"] == "chief"

  def _make_eval_prediction_hooks_fn(self):
    external_scorers = self._config["eval"].get("external_evaluators")
    if not self._config["eval"].get("save_eval_predictions", False) and external_scorers is None:
      return None
    if self._model.unsupervised:
      raise RuntimeError("This model does not support saving evaluation predictions")
    save_path = os.path.join(self._config["model_dir"], "eval")
    if not tf.io.gfile.exists(save_path):
      tf.io.gfile.makedirs(save_path)
    if external_scorers is not None:
      external_evaluator = evaluator.ExternalEvaluator(
          labels_file=self._config["data"]["eval_labels_file"],
          output_dir=os.path.join(self._config["model_dir"], "external_eval"),
          scorers=evaluator.make_scorers(external_scorers))
    else:
      external_evaluator = None
    return lambda predictions, step: [
        hooks.SaveEvaluationPredictionHook(
            self._model,
            predictions,
            step,
            os.path.join(save_path, "predictions.txt"),
            post_evaluation_fn=external_evaluator)]

  def _finalize_training_parameters(self):
    train_config = self._config["train"]
    batch_size = train_config.get("batch_size")

    # Auto tune batch size.
    if batch_size is None or batch_size == 0:
      if train_config["batch_type"] == "examples":
        raise ValueError("Batch size autotuning is only supported for the \"tokens\" batch type")
      max_batch_size = 16384
      if train_config.get("effective_batch_size") is not None:
        max_batch_size = min(max_batch_size, train_config["effective_batch_size"])
      train_config["batch_size"] = _auto_tune_batch_size(
          self._config,
          max_batch_size=max_batch_size,
          num_devices=self._num_devices)

    # Set gradients accumulation based on the requested effective batch size.
    if train_config.get("effective_batch_size") is not None:
      self._config["params"]["gradients_accum"] = _count_batch_accum(
          train_config["batch_size"],
          train_config["effective_batch_size"],
          num_replicas=self._num_replicas)
      tf.compat.v1.logging.info(
          "Accumulate gradients of %d iterations to reach effective batch size of %d",
          self._config["params"]["gradients_accum"],
          train_config["effective_batch_size"])

  def _build_train_spec(self, checkpoint_path):
    train_hooks = []
    if checkpoint_path is not None:
      # TODO: reimplement this hook for V2.
      train_hooks.append(hooks.LoadWeightsFromCheckpointHook(checkpoint_path))

    train_steps = self._config["train"].get("train_steps")
    train_spec = tf.estimator.TrainSpec(
        input_fn=estimator_util.make_input_fn(
            self._model,
            tf.estimator.ModeKeys.TRAIN,
            self._config["train"]["batch_size"],
            features_file=self._config["data"]["train_features_file"],
            labels_file=self._config["data"].get("train_labels_file"),
            batch_type=self._config["train"]["batch_type"],
            bucket_width=self._config["train"]["bucket_width"],
            maximum_features_length=self._config["train"].get("maximum_features_length"),
            maximum_labels_length=self._config["train"].get("maximum_labels_length"),
            shuffle_buffer_size=self._config["train"]["sample_buffer_size"],
            single_pass=self._config["train"].get("single_pass", False),
            num_threads=self._config["train"].get("num_threads"),
            prefetch_buffer_size=self._config["train"].get("prefetch_buffer_size")),
        max_steps=train_steps,
        hooks=train_hooks)
    return train_spec

  def _build_eval_spec(self):
    eval_spec = tf.estimator.EvalSpec(
        input_fn=estimator_util.make_input_fn(
            self._model,
            tf.estimator.ModeKeys.EVAL,
            self._config["eval"]["batch_size"],
            features_file=self._config["data"]["eval_features_file"],
            labels_file=self._config["data"].get("eval_labels_file"),
            num_threads=self._config["eval"].get("num_threads"),
            prefetch_buffer_size=self._config["eval"].get("prefetch_buffer_size")),
        steps=None,
        exporters=_make_exporters(
            self._config["eval"]["exporters"],
            estimator_util.make_serving_input_fn(self._model),
            assets_extra=self._get_model_assets()),
        throttle_secs=self._config["eval"]["eval_delay"])
    return eval_spec

  def _get_model_assets(self):
    generated_assets_path = os.path.join(self._config["model_dir"], "assets")
    if not tf.io.gfile.exists(generated_assets_path):
      tf.io.gfile.makedirs(generated_assets_path)
    return self._model.get_assets(asset_dir=generated_assets_path)

  def train_and_evaluate(self, checkpoint_path=None):
    """Runs the training and evaluation loop.

    Args:
      checkpoint_path: The checkpoint path to load the model weights from it.

    Returns:
      A tuple with a dict of evaluation metrics and the export result or
      ``None`` in TensorFlow 1.8 and older.
    """
    if checkpoint_path is not None and tf.io.gfile.isdir(checkpoint_path):
      checkpoint_path = tf.train.latest_checkpoint(checkpoint_path)
    self._finalize_training_parameters()
    train_spec = self._build_train_spec(checkpoint_path)
    eval_spec = self._build_eval_spec()
    estimator = self._make_estimator()
    result = tf.estimator.train_and_evaluate(estimator, train_spec, eval_spec)
    self._maybe_average_checkpoints()
    return result

  def train(self, checkpoint_path=None):
    """Runs the training loop.

    Args:
      checkpoint_path: The checkpoint path to load the model weights from it.

    Returns:
      The path to the final model directory.
    """
    self._finalize_training_parameters()
    params = self._config["params"]
    data_config = self._config["data"]
    train_config = self._config["train"]

    dataset = self._model.examples_inputter.make_training_dataset(
        data_config["train_features_file"],
        data_config.get("train_labels_file"),
        train_config["batch_size"],
        batch_type=train_config["batch_type"],
        shuffle_buffer_size=train_config["sample_buffer_size"],
        bucket_width=train_config["bucket_width"],
        maximum_features_length=train_config.get("maximum_features_length"),
        maximum_labels_length=train_config.get("maximum_labels_length"),
        single_pass=train_config.get("single_pass", False),
        num_threads=train_config.get("num_threads", 4),
        prefetch_buffer_size=train_config.get("prefetch_buffer_size"))

    optimizer = self._model.get_optimizer(params=params)
    gradients = []

    @tf.function(input_signature=dataset_util.input_signature_from_dataset(dataset))
    def _step(source, target):
      outputs, _ = self._model(source, target, params, tf.estimator.ModeKeys.TRAIN)
      loss = self._model.compute_loss(outputs, target, training=True, params=params)
      loss = loss[0] / loss[1]
      variables = self._model.trainable_variables
      step_gradients = optimizer.get_gradients(loss, variables)
      if not gradients:
        for step_gradient in step_gradients:
          gradients.append(tf.Variable(tf.zeros_like(step_gradient), trainable=False))
      for gradient, step_gradient in zip(gradients, step_gradients):
        gradient.assign_add(step_gradient)

      num_words = {}
      if "length" in source:
        num_words["source"] = tf.reduce_sum(source["length"])
      if "length" in target:
        num_words["target"] = tf.reduce_sum(target["length"])
      return loss, num_words

    @tf.function
    def _apply_gradients():
      variables = self._model.trainable_variables
      optimizer.apply_gradients(zip(gradients, variables))
      for gradient in gradients:
        gradient.assign(tf.zeros_like(gradient))

    train_steps = train_config.get("train_steps")
    output_dir = self._config["model_dir"]
    save_checkpoints_steps = train_config.get("save_checkpoints_steps", 5000)
    checkpoint = tf.train.Checkpoint(model=self._model, optimizer=optimizer)
    checkpoint_manager = tf.train.CheckpointManager(
        checkpoint,
        output_dir,
        train_config.get("keep_checkpoint_max", 8))
    if checkpoint_path is not None and tf.io.gfile.isdir(checkpoint_path):
      checkpoint_path = tf.train.latest_checkpoint(checkpoint_path)
    elif checkpoint_path is None and checkpoint_manager.latest_checkpoint is not None:
      checkpoint_path = checkpoint_manager.latest_checkpoint
    if checkpoint_path is not None:
      checkpoint.restore(checkpoint_path)
      tf.get_logger().info("Restored checkpoint %s", checkpoint_path)
      if train_steps is not None and optimizer.iterations.numpy() >= train_steps:
        tf.get_logger().warn("Model already reached train_steps = %d. Exiting.", train_steps)
        return output_dir

    accum_num_words = {}
    last_report_time = time.time()
    report_every = train_config.get("save_summary_steps", 100)
    accum_count = params.get("gradients_accum", 1)

    for i, (source, target) in enumerate(dataset):
      loss, num_words = _step(source, target)
      if i == 0 or (i + 1) % accum_count == 0:
        _apply_gradients()

      for key, value in six.iteritems(num_words):
        value = value.numpy()
        if key not in accum_num_words:
          accum_num_words[key] = value
        else:
          accum_num_words[key] += value

      step = optimizer.iterations.numpy()
      if step % report_every == 0:
        last_report_time = _report_training_status(
            step,
            loss,
            optimizer.learning_rate,
            accum_num_words,
            last_report_time)
      if step % save_checkpoints_steps == 0 or step == train_steps:
        path = checkpoint_manager.save(checkpoint_number=step)
        tf.get_logger().info("Saved checkpoint %s", path)
      if step == train_steps:
        break

    return self._maybe_average_checkpoints()

  def evaluate(self, checkpoint_path=None):
    """Runs evaluation.

    Args:
      checkpoint_path: The checkpoint path to load the model weights from it.

    Returns:
      A dict of evaluation metrics.
    """
    if checkpoint_path is not None and tf.io.gfile.isdir(checkpoint_path):
      checkpoint_path = tf.train.latest_checkpoint(checkpoint_path)
    eval_spec = self._build_eval_spec()
    estimator = self._make_estimator()
    return estimator.evaluate(
        eval_spec.input_fn, hooks=eval_spec.hooks, checkpoint_path=checkpoint_path)

  def _maybe_average_checkpoints(self, avg_subdirectory="avg"):
    """Averages checkpoints if enabled in the training configuration and if the
    current training instance is the chief.

    Args:
      avg_subdirectory: The directory within the model directory that will
        contain the averaged checkpoint.

    Returns:
      The path to the latest model directory.
    """
    average_last_checkpoints = self._config["train"].get("average_last_checkpoints", 0)
    model_dir = self._config["model_dir"]
    if average_last_checkpoints > 0 and self.is_chief():
      return self.average_checkpoints(
          os.path.join(model_dir, avg_subdirectory),
          max_count=average_last_checkpoints)
    return model_dir

  def average_checkpoints(self, output_dir, max_count=8):
    """Averages checkpoints.

    Args:
      output_dir: The directory that will contain the averaged checkpoint.
      max_count: The maximum number of checkpoints to average.

    Returns:
      The path to the directory containing the averaged checkpoint.
    """
    optimizer = self._model.get_optimizer(params=self._config["params"])
    # Create all variables.
    self._model.create_variables()
    _ = optimizer.iterations
    optimizer._create_hypers()
    optimizer._create_slots(self._model.trainable_variables)
    trackables = dict(model=self._model, optimizer=optimizer)
    return checkpoint.average_checkpoints(
        self._config["model_dir"],
        output_dir,
        trackables,
        max_count=max_count)

  def infer(self,
            features_file,
            predictions_file=None,
            checkpoint_path=None,
            log_time=False):
    """Runs inference.

    Args:
      features_file: The file(s) to infer from.
      predictions_file: If set, predictions are saved in this file.
      checkpoint_path: Path of a specific checkpoint to predict. If ``None``,
        the latest is used.
      log_time: If ``True``, several time metrics will be printed in the logs at
        the end of the inference loop.
    """
    params = self._config["params"]
    infer_config = self._config["infer"]
    dataset = self._model.examples_inputter.make_inference_dataset(
        features_file,
        infer_config["batch_size"],
        bucket_width=infer_config["bucket_width"],
        num_threads=infer_config.get("num_threads", 1),
        prefetch_buffer_size=infer_config.get("prefetch_buffer_size"))

    @tf.function(input_signature=(dataset_util.input_signature_from_dataset(dataset),))
    def _infer(source):
      _, predictions = self._model(source, None, params, tf.estimator.ModeKeys.PREDICT)
      return predictions

    _restore_checkpoint(self._model, self._config["model_dir"], checkpoint_path=checkpoint_path)

    if predictions_file:
      stream = io.open(predictions_file, encoding="utf-8", mode="w")
    else:
      stream = sys.stdout

    ordered_writer = None
    write_fn = lambda prediction: (
        self._model.print_prediction(prediction, params=infer_config, stream=stream))

    total_time = 0
    total_tokens = 0
    total_examples = 0

    for source in dataset:
      start_time = time.time()
      predictions = _infer(source)
      end_time = time.time()
      predictions = {k:v.numpy() for k, v in six.iteritems(predictions)}
      if log_time:
        total_time += end_time - start_time
        batch_size = next(six.itervalues(predictions)).shape[0]
        total_examples += batch_size
        length = predictions.get("length")
        if length is not None:
          if len(length.shape) == 2:
            length = length[:, 0]
          total_tokens += sum(length)
      for prediction in misc.extract_batches(predictions):
        if "index" in prediction:
          if ordered_writer is None:
            ordered_writer = misc.OrderRestorer(
                index_fn=lambda prediction: prediction["index"], callback_fn=write_fn)
          ordered_writer.push(prediction)
        else:
          write_fn(prediction)

    if log_time:
      tf.get_logger().info("Total prediction time (s): %f", total_time)
      tf.get_logger().info(
          "Average prediction time (s): %f", total_time / total_examples)
      if total_tokens > 0:
        tf.get_logger().info("Tokens per second: %f", total_tokens / total_time)
    if predictions_file:
      stream.close()

  def export(self, checkpoint_path=None, export_dir_base=None):
    """Exports a model.

    Args:
      checkpoint_path: The checkpoint path to export. If ``None``, the latest is used.
      export_dir_base: The base directory in which a timestamped subdirectory
        containing the exported model will be created. Defaults to
        ``$MODEL_DIR/export/manual``.

    Returns:
      The string path to the exported directory.
    """
    estimator = self._make_estimator()
    if checkpoint_path is not None and tf.io.gfile.isdir(checkpoint_path):
      checkpoint_path = tf.train.latest_checkpoint(checkpoint_path)
    if export_dir_base is None:
      export_dir_base = os.path.join(estimator.model_dir, "export", "manual")

    return estimator.export_saved_model(
        export_dir_base,
        estimator_util.make_serving_input_fn(self._model),
        assets_extra=self._get_model_assets(),
        checkpoint_path=checkpoint_path)

  def score(self, features_file, predictions_file, checkpoint_path=None, output_file=None):
    """Scores existing predictions.

    Args:
      features_file: The input file.
      predictions_file: The predictions file to score.
      checkpoint_path: Path of a specific checkpoint to use. If ``None``,
        the latest is used.
      output_file: The file where the scores are saved. Otherwise, they will be
        printed on the standard output.

    Raises:
      ValueError: if the model is not a sequence to sequence model or a
        language model.
      ValueError: if no checkpoint are found.
      ValueError: if :obj:`predictions_file` is not given.
    """
    if not isinstance(self._model, (models.LanguageModel, models.SequenceToSequence)):
      raise ValueError("scoring only works for sequence to sequence or language models")
    if isinstance(self._model, models.SequenceToSequence) and not predictions_file:
      raise ValueError("predictions_file is required when scoring with a "
                       "sequence to sequence model")

    _restore_checkpoint(self._model, self._config["model_dir"], checkpoint_path=checkpoint_path)

    params = self._config["params"]
    score_config = self._config["score"]
    dataset = self._model.examples_inputter.make_evaluation_dataset(
        features_file,
        predictions_file,
        score_config["batch_size"],
        num_threads=score_config.get("num_threads"),
        prefetch_buffer_size=score_config.get("prefetch_buffer_size"))

    @tf.function(input_signature=dataset_util.input_signature_from_dataset(dataset))
    def _score(features, labels):
      outputs, _ = self._model(source, target, params, tf.estimator.ModeKeys.EVAL)
      cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
          labels["ids_out"], outputs["logits"])
      weights = tf.sequence_mask(labels["length"], dtype=cross_entropy.dtype)
      masked_cross_entropy = cross_entropy * weights
      scores = tf.reduce_sum(masked_cross_entropy, axis=1)
      results = {
          "cross_entropy": cross_entropy,
          "score": scores,
          "tokens": labels["tokens"],
          "length": labels["length"] - 1  # -1 for the special token.
      }
      if "attention" in outputs:
        results["attention"] = outputs["attention"]
      return results

    if output_file:
      stream = io.open(output_file, encoding="utf-8", mode="w")
    else:
      stream = sys.stdout

    output_tokenizer = (
        self._model.labels_inputter.tokenizer if not self._model.unsupervised
        else self._model.features_inputter.tokenizer)

    for source, target in dataset:
      results = _score(source, target)
      results = {k:v.numpy() for k, v in six.iteritems(results)}
      for batch in misc.extract_batches(results):
        tokens = batch["tokens"][:batch["length"]]
        sentence = output_tokenizer.detokenize(tokens)
        token_level_scores = None
        attention = None
        if score_config.get("with_token_level"):
          token_level_scores = batch["cross_entropy"][:batch["length"]]
        if "attention" in batch:
          attention = batch["attention"][:batch["length"]]
        alignment_type = score_config.get("with_alignments")
        sentence = misc.format_translation_output(
            sentence,
            score=batch["score"],
            token_level_scores=token_level_scores,
            attention=attention,
            alignment_type=alignment_type)
        misc.print_bytes(tf.compat.as_bytes(sentence), stream=stream)

    if output_file:
      stream.close()

def _make_exporters(exporters_type, serving_input_fn, assets_extra=None):
  if exporters_type is None:
    return None
  if not isinstance(exporters_type, list):
    exporters_type = [exporters_type]
  exporters = []
  for exporter_type in exporters_type:
    exporter_type = exporter_type.lower()
    if exporter_type == "last":
      exporters.append(tf.estimator.LatestExporter(
          "latest", serving_input_fn, assets_extra=assets_extra))
    elif exporter_type == "final":
      exporters.append(tf.estimator.FinalExporter(
          "final", serving_input_fn, assets_extra=assets_extra))
    elif exporter_type == "best":
      exporters.append(tf.estimator.BestExporter(
          name="best", serving_input_receiver_fn=serving_input_fn, assets_extra=assets_extra))
    else:
      raise ValueError("invalid exporter type: %s" % exporter_type)
  if len(exporters) == 1:
    return exporters[0]
  return exporters

def _count_batch_accum(batch_size, target_batch_size, num_replicas=1):
  """Given the current batch size, the number of replicas, and the requested
  effective batch size, returns the number of gradients to accumulate.
  """
  return int(math.ceil(float(target_batch_size) / (batch_size * num_replicas)))

def _auto_tune_batch_size(config,
                          min_batch_size=1024,
                          max_batch_size=16384,
                          min_range=256,
                          sample_iterations=5,
                          num_devices=1,
                          gpu_memory_fraction=0.8):
  """Find the largest token-based batch size that can be used with this
  configuration.

  This function runs some training iterations and uses out-of-memory errors as
  search conditions. A binary search is used to converge to a suitable batch
  size.

  We prefer to run the iterations in a different process so that it does not
  alter the current context (OOM may not be safe to recover from, see for
  example https://stackoverflow.com/q/53820713/2529808).

  Args:
    config: The training configuration.
    min_batch_size: The smallest batch size to consider.
    max_batch_size: The largest batch size to consider.
    min_range: Continue searching while the difference between
      :obj:`max_batch_size` and :obj:`min_batch_size` is larger than this value.
    sample_iterations: The number of training iterations.
    num_devices: The number of devices to use.
    gpu_memory_fraction: Fraction of the GPU memory to use.

  Returns:
    The autotuned batch size.
  """
  config = copy.deepcopy(config)
  config["train"]["save_checkpoints_steps"] = None
  config["train"]["average_last_checkpoints"] = 0
  config["train"]["train_steps"] = sample_iterations
  config_path = os.path.join(config["model_dir"], "batch_size_autotuner.yml")

  # Define the TensorFlow session config, if needed.
  session_config_path = None
  if gpu_memory_fraction < 1:
    session_config = tf.compat.v1.ConfigProto(
        gpu_options=tf.compat.v1.GPUOptions(per_process_gpu_memory_fraction=gpu_memory_fraction))
    session_config_path = os.path.join(config["model_dir"], "batch_size_autotuner.proto")
    with tf.io.gfile.GFile(session_config_path, mode="w") as session_config_file:
      session_config_file.write(text_format.MessageToString(session_config))

  args = [
      "python", "-m", "opennmt.bin.main", "train",
      "--config", config_path, "--num_gpus", str(num_devices)]
  if session_config_path is not None:
    args += ["--session_config", session_config_path]

  tf.compat.v1.logging.info(
      "Searching the largest batch size between %d and %d with a precision of %d...",
      min_batch_size, max_batch_size, min_range)

  while max_batch_size - min_batch_size > min_range:
    batch_size = (max_batch_size + min_batch_size) // 2

    # Update configuration with current batch size and adjusted gradients
    # accumulation.
    config["train"]["batch_size"] = batch_size
    if config["train"].get("effective_batch_size") is not None:
      config["params"]["gradients_accum"] = _count_batch_accum(
          batch_size, config["train"]["effective_batch_size"], num_replicas=num_devices)
    with tf.io.gfile.GFile(config_path, mode="wb") as config_file:
      yaml.dump(config, config_file)

    tf.compat.v1.logging.info("Trying training with batch size %d...", batch_size)
    with open(os.devnull, "w") as devnull:
      process = subprocess.Popen(args, stdout=devnull, stderr=devnull)
      exit_code = process.wait()

    if exit_code != 0:
      tf.compat.v1.logging.info("... failed.")
      max_batch_size = batch_size - 1
    else:
      tf.compat.v1.logging.info(
          "... succeeded, continue until the search range is smaller than %d.", min_range)
      min_batch_size = batch_size

  tf.compat.v1.logging.info("Batch size auto tuned to %d.", min_batch_size)

  # Cleanup temporary files.
  os.remove(config_path)
  if session_config_path is not None:
    os.remove(session_config_path)
  return min_batch_size

def _report_training_status(step, loss, learning_rate, accum_num_words, last_report_time):
  new_report_time = time.time()
  words_per_sec_fmt = []
  for key, value in six.iteritems(accum_num_words):
    avg = value / (new_report_time - last_report_time)
    accum_num_words[key] = 0
    fmt = "%s words/s = %d" % (key, int(avg))
    words_per_sec_fmt.append(fmt)
  if isinstance(learning_rate, tf.optimizers.schedules.LearningRateSchedule):
    learning_rate = learning_rate(step)
  tf.get_logger().info(
      "Step = %d ; %s ; Learning rate = %f ; Loss = %f",
      step,
      ", ".join(words_per_sec_fmt),
      learning_rate,
      loss)
  return new_report_time

def _restore_checkpoint(model, model_dir, checkpoint_path=None):
  checkpoint = tf.train.Checkpoint(model=model)
  if checkpoint_path is None:
    checkpoint_path = model_dir
  if tf.io.gfile.isdir(checkpoint_path):
    checkpoint_path = tf.train.latest_checkpoint(checkpoint_path)
  checkpoint.restore(checkpoint_path)
  tf.get_logger().info("Restored checkpoint %s", checkpoint_path)
  return checkpoint_path
