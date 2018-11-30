"""GaussianPolicy."""

from collections import OrderedDict

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from softlearning.distributions.squash_bijector import SquashBijector
from softlearning.models.feedforward import feedforward_model

from .base_policy import BasePolicy


SCALE_DIAG_MIN_MAX = (-20, 2)


class GaussianPolicy(BasePolicy):
    def __init__(self,
                 input_shapes,
                 output_shape,
                 hidden_layer_sizes,
                 squash=True,
                 activation='relu',
                 output_activation='linear',
                 name=None,
                 *args,
                 **kwargs):
        super(GaussianPolicy, self).__init__(*args, **kwargs)
        self._Serializable__initialize(locals())

        self._squash = squash

        self.condition_inputs = [
            tf.keras.layers.Input(shape=input_shape)
            for input_shape in input_shapes
        ]

        conditions = tf.keras.layers.Lambda(
            lambda x: tf.concat(x, axis=-1)
        )(self.condition_inputs)

        shift_and_log_scale_diag = feedforward_model(
            input_shapes=(conditions.shape[1:], ),
            hidden_layer_sizes=hidden_layer_sizes,
            output_size=output_shape[0] * 2,
            activation=activation,
            output_activation=output_activation,
            *args,
            **kwargs
        )(conditions)

        shift, log_scale_diag = tf.keras.layers.Lambda(
            lambda shift_and_log_scale_diag: tf.split(
                shift_and_log_scale_diag,
                num_or_size_splits=2,
                axis=-1)
        )(shift_and_log_scale_diag)

        log_scale_diag = tf.keras.layers.Lambda(
            lambda log_scale_diag: tf.clip_by_value(
                log_scale_diag, *SCALE_DIAG_MIN_MAX)
        )(log_scale_diag)

        batch_size = tf.keras.layers.Lambda(
            lambda x: tf.shape(x)[0])(conditions)

        base_distribution = tfp.distributions.MultivariateNormalDiag(
            loc=tf.zeros(output_shape),
            scale_diag=tf.ones(output_shape))

        latents = tf.keras.layers.Lambda(
            lambda batch_size: base_distribution.sample(batch_size)
        )(batch_size)

        def raw_actions_fn(inputs):
            shift, log_scale_diag, latents = inputs
            bijector = tfp.bijectors.Affine(
                shift=shift,
                scale_diag=tf.exp(log_scale_diag))
            actions = bijector.forward(latents)
            return actions

        raw_actions = tf.keras.layers.Lambda(
            raw_actions_fn
        )((shift, log_scale_diag, latents))

        squash_bijector = (
            SquashBijector()
            if self._squash
            else tfp.bijectors.Identity())

        actions = tf.keras.layers.Lambda(
            lambda raw_actions: squash_bijector.forward(raw_actions)
        )(raw_actions)

        self.actions_model = tf.keras.Model(self.condition_inputs, actions)

        deterministic_actions = tf.keras.layers.Lambda(
            lambda shift: squash_bijector.forward(shift)
        )(shift)

        self.deterministic_actions_model = tf.keras.Model(
            self.condition_inputs, deterministic_actions)

        def log_pis_fn(inputs):
            shift, log_scale_diag, actions = inputs
            base_distribution = tfp.distributions.MultivariateNormalDiag(
                loc=tf.zeros(output_shape),
                scale_diag=tf.ones(output_shape))
            bijector = tfp.bijectors.Chain((
                squash_bijector,
                tfp.bijectors.Affine(
                    shift=shift,
                    scale_diag=tf.exp(log_scale_diag)),
            ))
            distribution = (
                tfp.distributions.ConditionalTransformedDistribution(
                    distribution=base_distribution,
                    bijector=bijector))

            log_pis = distribution.log_prob(actions)[:, None]
            return log_pis

        self.actions_input = tf.keras.layers.Input(shape=output_shape)

        log_pis = tf.keras.layers.Lambda(
            log_pis_fn)([shift, log_scale_diag, actions])

        log_pis_for_action_input = tf.keras.layers.Lambda(
            log_pis_fn)([shift, log_scale_diag, self.actions_input])

        self.log_pis_model = tf.keras.Model(
            (*self.condition_inputs, self.actions_input),
            log_pis_for_action_input)

        self.diagnostics_model = tf.keras.Model(
            self.condition_inputs,
            (shift, log_scale_diag, log_pis, raw_actions, actions))

    def get_weights(self):
        return self.actions_model.get_weights()

    def set_weights(self, *args, **kwargs):
        return self.actions_model.set_weights(*args, **kwargs)

    @property
    def trainable_variables(self):
        return self.actions_model.trainable_variables

    @property
    def non_trainable_weights(self):
        """Due to our nested model structure, we need to filter duplicates."""
        return list(set(super(GaussianPolicy, self).non_trainable_weights))

    def reset(self):
        pass

    def actions(self, conditions):
        if self._deterministic:
            raise NotImplementedError

        return self.actions_model(conditions)

    def log_pis(self, conditions, actions):
        assert not self._deterministic, self._deterministic
        return self.log_pis_model([*conditions, actions])

    def actions_np(self, conditions):
        if self._deterministic:
            return self.deterministic_actions_model.predict(conditions)
        else:
            return self.actions_model.predict(conditions)

    def log_pis_np(self, conditions, actions):
        assert not self._deterministic, self._deterministic
        return self.log_pis_model.predict([*conditions, actions])

    def get_diagnostics(self, conditions):
        """Return diagnostic information of the policy.

        Returns the mean, min, max, and standard deviation of means and
        covariances.
        """
        (shifts_np,
         log_scale_diags_np,
         log_pis_np,
         raw_actions_np,
         actions_np) = self.diagnostics_model.predict(conditions)

        return OrderedDict({
            'shifts-mean': np.mean(shifts_np),
            'shifts-std': np.std(shifts_np),

            'log_scale_diags-mean': np.mean(log_scale_diags_np),
            'log_scale_diags-std': np.std(log_scale_diags_np),

            '-log-pis-mean': np.mean(-log_pis_np),
            '-log-pis-std': np.std(-log_pis_np),

            'raw-actions-mean': np.mean(raw_actions_np),
            'raw-actions-std': np.std(raw_actions_np),

            'actions-mean': np.mean(actions_np),
            'actions-std': np.std(actions_np),
        })
