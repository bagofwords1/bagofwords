"""RunFailure (refhist01): why a run of a release's copies stopped -- the text
Publish and the dashboard show, with its two parts kept for the refresh
history. It travels in result dicts and alerts, so it must survive whatever
copies or serializes those."""
import copy
import pickle

from app.services.dashboard_publish import RunFailure


def test_a_run_failure_reads_as_the_text_it_always_was():
    failure = RunFailure("Sales DB", "the token expired")
    assert failure == "Sales DB: the token expired" and isinstance(failure, str)
    assert (failure.source, failure.reason) == ("Sales DB", "the token expired")


def test_a_run_failure_survives_copy_and_pickle():
    failure = RunFailure("הכנסות לפי חודש", "relation \"orders\" does not exist")
    for again in (copy.copy(failure), copy.deepcopy(failure), pickle.loads(pickle.dumps(failure)),
                  copy.deepcopy({"error": failure})["error"]):
        assert again == failure
        assert (again.source, again.reason) == (failure.source, failure.reason)
