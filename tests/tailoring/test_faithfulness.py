"""Faithfulness guard."""
import pytest

from careerops.tailoring.faithfulness import check
from careerops.tailoring.scorer import Keyword

KW = [Keyword("Kubernetes", 1, False), Keyword("docker", 2, True), Keyword("mlflow", 1, False)]
SRC = "Deployed the agent as a Docker container on AWS Lambda with ECR image pipelines."


def test_guard_accepts_faithful_rephrase():
    assert check("Containerised and deployed the agent with Docker on AWS Lambda via ECR pipelines.",
                 SRC, ["containerised"], ["kubernetes"], KW).ok


@pytest.mark.parametrize("bad, why", [
    ("Deployed the agent with Docker and Kubernetes on AWS Lambda.", "deny"),
    ("Deployed the agent with Docker on AWS Lambda, cutting latency 40%.", "numbers"),
    ("Deployed the agent with Docker on AWS Lambda, tracked in MLflow.", "keyword"),
])
def test_guard_rejects_fabrication(bad, why):
    c = check(bad, SRC, [], ["kubernetes"], KW)
    assert not c.ok and any(why in v for v in c.violations), c.violations
