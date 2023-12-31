import jax
import jax.numpy as jnp

from typing import Any


# @partial(jax.jit, static_argnames=("policy_fn", "horizon"))
def policy_rollout(policy_fn, rng_key, mu_vectors, horizon):
    """Returns a rollout of the policy, i.e. a sequence of actions and rewards.

    Args:
      policy_fn: A function that takes rng_key, timesteps, actions, rewards and
      returns a log-probability distribution over actions.
      rng_key: A JAX random key.
      mu_vectors: Samples from the prior over means, shape (n_samples, num_actions).
      horizon: The length of the rollout.

    # TODO optimize more
    Returns:
        actions: The sequence of actions, shape (n_samples, horizon).
        rewards: The sequence of rewards, shape (n_samples, horizon).
        log_probs: The log-probabilities of the taken actions, shape (n_samples, horizon, num_actions).
    """
    n_envs = mu_vectors.shape[0]

    @jax.remat
    def policy_step(state_input, _):
        i, time_steps, actions, rewards, rngs = state_input
        # TODO make the transformer input dynamic

        # shape: (n_envs, num_actions)
        log_prob = policy_fn(rngs[i, 0], time_steps, actions, rewards)
        new_action = jax.random.categorical(rngs[i, 1], log_prob)

        new_reward = jax.random.bernoulli(
            rngs[i, 2], mu_vectors[jnp.arange(n_envs), new_action]
        )

        actions = actions.at[:, i + 1].set(new_action)
        time_steps = time_steps.at[:, i + 1].set(i + 1)
        rewards = rewards.at[:, i + 1].set(new_reward)
        carry = (i + 1, time_steps, actions, rewards, rngs)
        return carry, log_prob

    # The first step is considered 0 with zero action and reward
    # However, history starts from 1
    rngs = jax.random.split(rng_key, 3 * (horizon + 1)).reshape(horizon + 1, 3, -1)
    init_val = (
        0,
        jnp.zeros((n_envs, horizon + 1), dtype=jnp.int32),
        jnp.zeros((n_envs, horizon + 1), dtype=jnp.int32),
        jnp.zeros((n_envs, horizon + 1), dtype=jnp.float32),
        rngs,
    )

    (_, _, actions, rewards, _), log_probs = jax.lax.scan(
        policy_step, init_val, (), length=horizon + 1
    )

    return (
        actions[:, 1:],
        rewards[:, 1:],
        jnp.transpose(log_probs, axes=(1, 0, 2))[:, 1:, :],
    )
