from typing import Dict, Tuple

from aesara.graph.basic import Variable
from aesara.graph.fg import FunctionGraph
from aesara.tensor.random.utils import RandomStream
from aesara.tensor.var import TensorVariable

from aemcmc.rewriting import (
    SamplerTracker,
    construct_ir_fgraph,
    expand_subsumptions,
    sampler_rewrites_db,
)


def construct_sampler(
    obs_rvs_to_values: Dict[TensorVariable, TensorVariable], srng: RandomStream
) -> Tuple[
    Dict[TensorVariable, TensorVariable],
    Dict[Variable, Variable],
    Dict[TensorVariable, TensorVariable],
]:
    r"""Eagerly construct a sampler for a given set of observed variables and their observations.

    Parameters
    ==========
    obs_rvs_to_values
        A ``dict`` of variables that maps stochastic elements
        (e.g. `RandomVariable`\s) to symbolic `Variable`\s representing their
        observed values.

    Returns
    =======
    A ``dict`` that maps each random variable to its sampler step and
    any updates generated by the sampler steps.
    """

    fgraph, obs_rvs_to_values, memo, new_to_old_rvs = construct_ir_fgraph(
        obs_rvs_to_values
    )

    fgraph.attach_feature(SamplerTracker(srng))

    _ = sampler_rewrites_db.query("+basic").rewrite(fgraph)

    random_vars = tuple(rv for rv in fgraph.outputs if rv not in obs_rvs_to_values)

    discovered_samplers = fgraph.sampler_mappings.rvs_to_samplers

    rvs_to_init_vals = {rv: rv.clone() for rv in random_vars}
    posterior_sample_steps = rvs_to_init_vals.copy()
    # Replace occurrences of observed variables with their observed values
    posterior_sample_steps.update(obs_rvs_to_values)

    # TODO FIXME: Get/extract `Scan`-generated updates
    posterior_updates: Dict[Variable, Variable] = {}

    rvs_without_samplers = set()

    for rv in fgraph.outputs:

        if rv in obs_rvs_to_values:
            continue

        rv_steps = discovered_samplers.get(rv)

        if not rv_steps:
            rvs_without_samplers.add(rv)
            continue

        # TODO FIXME: Just choosing one for now, but we should consider them all.
        step_desc, step, updates = rv_steps.pop()

        # Expand subsumed `DimShuffle`d inputs to `Elemwise`s
        if updates:
            update_keys, update_values = zip(*updates.items())
        else:
            update_keys, update_values = tuple(), tuple()

        sfgraph = FunctionGraph(
            outputs=(step,) + tuple(update_keys) + tuple(update_values),
            clone=False,
            copy_inputs=False,
            copy_orphans=False,
        )

        # Update the other sampled random variables in this step's graph
        sfgraph.replace_all(list(posterior_sample_steps.items()), import_missing=True)

        expand_subsumptions.rewrite(sfgraph)

        step = sfgraph.outputs[0]

        # Update the other sampled random variables in this step's graph
        # (step,) = clone_replace([step], replace=posterior_sample_steps)

        posterior_sample_steps[rv] = step

        if updates:
            keys_offset = len(update_keys) + 1
            update_keys = sfgraph.outputs[1:keys_offset]
            update_values = sfgraph.outputs[keys_offset:]
            updates = dict(zip(update_keys, update_values))
            posterior_updates.update(updates)

    if rvs_without_samplers:
        # TODO: Assign NUTS to these
        raise NotImplementedError(
            f"Could not find a posterior samplers for {rvs_without_samplers}"
        )

    # TODO: Track/handle "auxiliary/augmentation" variables introduced by sample
    # steps?

    return (
        {
            new_to_old_rvs[rv]: step
            for rv, step in posterior_sample_steps.items()
            if rv not in obs_rvs_to_values
        },
        posterior_updates,
        {new_to_old_rvs[rv]: init_var for rv, init_var in rvs_to_init_vals.items()},
    )
