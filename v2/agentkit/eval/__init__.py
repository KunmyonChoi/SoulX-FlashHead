"""Evaluation (WP6): post-conversation analysis, simulated customers, test suites, comparison.

    from agentkit.eval import analyze_session, run_persona, run_case, run_suite, compare, save_case
    from agentkit.eval.api import router

Every function takes its collaborators (store, definition_store, llm, driver_factory) as optional
keyword arguments and falls back to agentkit.trace / agentkit.definition / agentkit.runtime.
"""
from .analysis import analyze_session
from .deps import configure
from .simulate import SimulatedCustomer, run_case, run_persona, verdict
from .suite import compare, list_runs, load_run, run_suite, save_case

__all__ = ["analyze_session", "run_persona", "run_case", "run_suite", "compare", "save_case",
           "list_runs", "load_run", "configure", "SimulatedCustomer", "verdict"]
