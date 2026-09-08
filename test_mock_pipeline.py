"""
One-off local test of the annotate_AbsBench.py pipeline using a MOCK Azure
client instead of a real one. No network calls, no API key needed, no cost.

The mock always answers every question with the fixed range [-3, +3], so we
can check that the full pipeline (batch build -> submit -> poll -> retrieve ->
parse -> save CSV) works end-to-end and produces a correct CSV.

Run from the repo root with:
    python test_mock_pipeline.py
"""

import io
import json
from types import SimpleNamespace

import pandas as pd

from src import annotation_utils as annutils
from src import azure_utils as azutils


# ---------------------------------------------------------------------------
# Mock Azure OpenAI client
# ---------------------------------------------------------------------------
# Real Azure batch flow: upload a JSONL file of requests -> create a batch job
# -> poll it until status == "completed" -> download the JSONL of results.
# This mock does the same three steps, but answers instantly and always with
# the same text: "<FINAL_RANGE>[-3, +3]</FINAL_RANGE>"

class MockFiles:
    def __init__(self):
        self._store = {}
        self._counter = 0

    def create(self, file, purpose):
        self._counter += 1
        file_id = f"mock-file-{self._counter}"
        content = file.read()
        self._store[file_id] = content
        return SimpleNamespace(id=file_id)

    def content(self, file_id):
        # The real SDK's .content(...).text is already a decoded str, so
        # match that here (our _store keeps raw bytes, like a real upload).
        raw = self._store[file_id]
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        return SimpleNamespace(text=text)


class MockBatches:
    def __init__(self, files: MockFiles):
        self._files = files
        self._counter = 0
        self._batches = {}

    def create(self, input_file_id, endpoint, completion_window):
        self._counter += 1
        batch_id = f"mock-batch-{self._counter}"

        raw = self._files._store[input_file_id]
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        requests = [json.loads(line) for line in text.strip().splitlines()]

        output_lines = []
        for req in requests:
            output_lines.append(json.dumps({
                "custom_id": req["custom_id"],
                "response": {
                    "body": {
                        "output": [
                            {"content": [
                                {"type": "output_text",
                                 "text": "The instance shows mild indicators. "
                                         "<FINAL_RANGE>[-3, +3]</FINAL_RANGE>"}
                            ]}
                        ],
                        "usage": {"total_tokens": 42}
                    }
                }
            }))
        output_text = "\n".join(output_lines).encode("utf-8")
        output_file_id = self._files.create(file=io.BytesIO(output_text), purpose="batch_output").id

        self._batches[batch_id] = SimpleNamespace(
            id=batch_id, status="completed",
            output_file_id=output_file_id, error_file_id=None,
        )
        return self._batches[batch_id]

    def retrieve(self, batch_id):
        return self._batches[batch_id]


class MockAzureClient:
    def __init__(self):
        self.files = MockFiles()
        self.batches = MockBatches(self.files)


# ---------------------------------------------------------------------------
# Run the same steps as scripts/annotate_AbsBench.py, but small and mocked
# ---------------------------------------------------------------------------

DIMENSIONS = {
    "extraversion": "Ex",
    "risk aversion": "RA",
    "delay of gratification": "TD",
    "ultracrepidarianism": "Ul",
    # "coopetition": "Co",  # excluded: rubrics/Co_v1.md is empty, no rubric written yet
}

SYS_PROMPT = "You are an expert at analyzing questions for cognitive biases."
N_SAMPLE = 5  # small on purpose, this is just a smoke test

if __name__ == "__main__":
    with open("rubrics/presentation.txt", "r") as f:
        PRES_PROMPT = f.read()

    ds = "AbstentionBench"
    df = pd.read_json(f"data/benchmarks/{ds}/{ds}.jsonl", lines=True)
    df["instance_id"] = [f"AbstentionBench_{i}" for i in range(df.shape[0])]
    df = df.sample(N_SAMPLE, random_state=42)

    client = MockAzureClient()

    for prop_name, prop_code in DIMENSIONS.items():
        ANN_PROMPT = (
            f"The following is a rubric for determining the propensity of "
            f"showing propensity towards {prop_name}:\n\n<rubric>\n"
        )

        with open(f"rubrics/{prop_code}_v1.md", encoding="ISO-8859-1") as f:
            RUBRIC = f.read()

        anns = []
        for i, row in df.iterrows():
            ann = annutils.PropensityAnnotation(
                propensity=prop_code,
                system_prompt=SYS_PROMPT + ANN_PROMPT,
                presentation_prompt=PRES_PROMPT,
                rubric=RUBRIC,
                task_prompt=row.question,
                source="gpt-5.1",
                metadata={"custom_id": row.instance_id},
            )
            anns.append(ann)

        annotations = annutils.PropAnnotationCollection(
            anns,
            error_filename=f"mock_errors_{prop_code}.jsonl",
            output_filename=f"mock_outputs_{prop_code}.jsonl",
        )

        annotations.annotate_batch(
            client=client,
            custom_ids="metadata",
            verbosity=2,
            timeout=60,
            schema="free",
            regex=r"<FINAL_RANGE>\s*\[\s*([+-]?\d+)\s*,\s*([+-]?\d+)\s*\]\s*</FINAL_RANGE>",
        )

        out_csv = f"data/annotations/{ds}/MOCK_ft_{ds}_{prop_code}_GPT-5.1_annotated2.csv"
        annotations.save_csv(out_csv, fields=[
            "propensity", "system_prompt", "task_prompt", "custom_id",
            "source", "lower_bound", "upper_bound", "explanation",
        ], index=False)

        print(f"[{prop_code}] wrote {out_csv}")

    print("MOCK TEST COMPLETE - no real Azure call was made.")
