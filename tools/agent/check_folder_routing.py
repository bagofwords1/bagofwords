"""Score the live provider separately from the deterministic 300-agent integration test."""

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.ai.context.agent_roster import FOLDER_ROUTING_GUIDANCE


def main():
    cases = [
        ("Attribution", "Break down what we paid to win customers by channel.", "A"),
        ("Paid Media", "Break down what we paid to win customers by channel.", "P"),
        ("Attribution", "Which team can work out CAC for our spring launch?", "A"),
        ("Paid Media", "Compare cost per acquisition across paid channels.", "P"),
        ("Attribution", "Which agent should answer this?", "clarify"),
        ("Paid Media", "Can you tell me about renewal cohorts?", "clarify"),
    ]
    passed = 0
    for agent, question, expected in cases:
        system = (
            "You route requests in the Marketing folder. Its published instruction says: "
            f"for acquisition cost and CAC select {agent}. Available agents: P Paid Media "
            "(channel spend and campaign efficiency), A Attribution (channel credit and conversion cost). "
            "If there is insufficient intent or no match ask for clarification. Output only the stable ID "
            f"(P or A), or clarify. {FOLDER_ROUTING_GUIDANCE} Return exactly one of the three values, "
            "using clarify when you would ask a question."
        )
        payload = {
            "model": os.environ.get("FOLDER_ROUTING_MODEL", "gpt-4.1-mini"),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": question}],
            "temperature": 0,
            "max_tokens": 8,
        }
        request = Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY_TEST"], "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=35) as response:
            actual = json.load(response)["choices"][0]["message"]["content"].strip()
        print(json.dumps(dict(question=question, rule=agent, expected=expected, actual=actual)), flush=True)
        passed += actual == expected
    print(f"{passed}/{len(cases)} exact matches")
    return int(passed != len(cases))


if __name__ == "__main__":
    sys.exit(main())
