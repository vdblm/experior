import jax

import flax.linen as nn
import jax.numpy as jnp
import jax.scipy.stats as jstats

from flax.training import train_state

from src.configs import ExperiorConfig
from src.commons import TransformerBlock

from abc import ABC, abstractmethod


##################### Priors #####################
def get_prior(conf: ExperiorConfig):
    if conf.prior.__class__.__name__ == "BetaPriorConfig":
        return BetaPrior
    else:
        raise NotImplementedError


class Prior(ABC):
    @abstractmethod
    def sample(self, rng_key, size):
        """Returns a sample from the prior."""
        pass


class BetaPrior(nn.Module, Prior):
    """A beta prior distribution over arm rewards for a Bernoulli bandit."""

    config: ExperiorConfig

    def setup(self):
        if self.config.prior.init_alpha is None:

            def alpha_init_fn(rng, shape):
                return 5.0 * jax.random.uniform(rng) * jnp.ones(shape)

        else:

            def alpha_init_fn(rng, shape):
                return self.config.prior.init_alpha * jnp.ones(shape)

        if self.config.prior.init_beta is None:

            def beta_init_fn(rng, shape):
                return 5.0 * jax.random.uniform(rng) * jnp.ones(shape)

        else:

            def beta_init_fn(rng, shape):
                return self.config.prior.init_beta * jnp.ones(shape)

        self.alphas_sq = self.param(
            "alphas_sq", alpha_init_fn, (self.config.prior.num_actions,)
        )
        self.betas_sq = self.param(
            "betas_sq", beta_init_fn, (self.config.prior.num_actions,)
        )

    def log_prob(self, mu):
        """Returns the log probability of a given mean vector."""
        alphas = jnp.power(self.alphas_sq, 2) + self.config.prior.epsilon
        betas = jnp.power(self.betas_sq, 2) + self.config.prior.epsilon
        return jstats.beta.logpdf(mu, alphas, betas)

    def __call__(self, mu):
        return self.log_prob(mu)

    def sample(self, rng_key, size):
        """Returns a sample from the prior."""
        alphas = self.alphas_sq**2 + self.config.prior.epsilon
        betas = self.betas_sq**2 + self.config.prior.epsilon
        return jax.random.beta(
            rng_key, a=alphas, b=betas, shape=(size, self.config.prior.num_actions)
        )

    @classmethod
    def create_state(
        cls, rng_key, optimizer, conf: ExperiorConfig
    ) -> train_state.TrainState:
        """Returns an initial state for the prior."""
        prior_model = cls(config=conf)
        variables = prior_model.init(rng_key, jnp.ones((1, conf.prior.num_actions)))

        prior_state = train_state.TrainState.create(
            apply_fn=prior_model.apply, params=variables["params"], tx=optimizer
        )

        return prior_state


##################### Policies #####################
def get_policy(conf: ExperiorConfig):
    if conf.policy.__class__.__name__ == "TransformerPolicyConfig":
        return TransformerPolicy
    else:
        raise NotImplementedError


class Policy(ABC):
    @abstractmethod
    def __call__(self, rng_key, timesteps, actions, rewards):
        """Returns the log-probability distribution over actions for a given history of steps.

        Args:
            rng_key: A JAX random key.
            timesteps: The history of timesteps, shape (batch_size, T).
            actions: The history of actions, shape (batch_size, T).
            rewards: The history of rewards, shape (batch_size, T).

        """
        pass


class TransformerPolicy(nn.Module, Policy):
    """A policy that takes the history of actions and rewards as input and outputs a probability distribution
    over actions. Inspired by:
    https://github.com/nikhilbarhate99/min-decision-transformer/blob/master/decision_transformer/model.py
    """

    config: ExperiorConfig
    name = "TransformerPolicy"

    @nn.compact
    def __call__(self, rng_key, timesteps, actions, rewards):
        """Returns the log-probability distribution over actions for a given history of steps.

        Args:
            rng_key: A JAX random key.
            timesteps: The history of timesteps, shape (batch_size, T).
            actions: The history of actions, shape (batch_size, T).
            rewards: The history of rewards, shape (batch_size, T).

        """
        B, T = timesteps.shape
        assert (
            T <= self.config.trainer.max_horizon
        ), f"Expected a history of at most {self.config.trainer.max_horizon} steps, got {T}"

        # shape: (B, T, h_dim)
        time_embedding = nn.Embed(
            num_embeddings=self.config.trainer.max_horizon,
            features=self.config.policy.h_dim,
            dtype=self.config.policy.dtype,
        )(timesteps)

        action_embedding = (
            nn.Embed(
                num_embeddings=self.config.prior.num_actions,
                features=self.config.policy.h_dim,
                dtype=self.config.policy.dtype,
            )(actions)
            + time_embedding
        )

        reward_embedding = (
            nn.Dense(features=self.config.policy.h_dim, dtype=self.config.policy.dtype)(
                rewards[..., jnp.newaxis]
            )
            + time_embedding
        )

        # sequence of (r0, a0, r1, a1, ...)
        h = jnp.stack([reward_embedding, action_embedding], axis=2).reshape(
            B, T * 2, self.config.policy.h_dim
        )

        class_token = self.param(
            "class_token",
            nn.initializers.normal(stddev=1e-6),
            (1, 1, self.config.policy.h_dim),
        )
        class_token = jnp.tile(class_token, (B, 1, 1))
        # shape: (B, T * 2 + 1, h_dim)
        h = jnp.concatenate([class_token, h], axis=1)

        h = nn.LayerNorm(dtype=self.config.policy.dtype)(h)

        h = nn.Sequential(
            [
                TransformerBlock(
                    h_dim=self.config.policy.h_dim,
                    num_heads=self.config.policy.num_heads,
                    drop_p=self.config.policy.drop_p,
                    dtype=self.config.policy.dtype,
                )
                for _ in range(self.config.policy.n_blocks)
            ]
        )(h)

        h = h[:, 0].reshape(B, self.config.policy.h_dim)
        action_logits = nn.Dense(
            features=self.config.prior.num_actions, dtype=self.config.policy.dtype
        )(h)
        log_probs = nn.log_softmax(action_logits)
        return log_probs  # shape: (B, num_actions)

    @classmethod
    def create_state(
        cls, rng_key, optimizer, conf: ExperiorConfig
    ) -> train_state.TrainState:
        """Returns an initial state for the policy."""
        policy = cls(config=conf)
        key1, key2 = jax.random.split(rng_key)
        variables = policy.init(
            key1,
            key2,
            jnp.ones((1, 2), dtype=jnp.int32),
            jnp.ones((1, 2), dtype=jnp.int32),
            jnp.ones((1, 2)),
        )

        policy_state = train_state.TrainState.create(
            apply_fn=policy.apply, params=variables["params"], tx=optimizer
        )

        return policy_state