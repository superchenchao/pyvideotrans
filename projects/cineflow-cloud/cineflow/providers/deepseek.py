from __future__ import annotations

import json

import httpx

from ..models import (
    CandidateScore,
    JobRequest,
    LineEvidence,
    SubtitleLine,
    Transcript,
)


class DeepSeekTranslator:
    """Standalone DeepSeek client matching the existing project's defaults.

    No code is imported from the parent pyVideoTrans project. The default model,
    OpenAI-compatible endpoint, large completion allowance and disabled thinking
    mode intentionally mirror the existing DeepSeek channel.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        max_tokens: int = 65536,
        thinking: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeek API key is required in production mode")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _body(self, *, prompt: dict, temperature: float, system: str) -> dict:
        return {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }

    async def _post_json(self, body: dict, *, timeout: float) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                self.chat_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    async def translate(self, request: JobRequest, transcript: Transcript) -> Transcript:
        if request.translation_engine != "deepseek":
            raise ValueError("this release supports DeepSeek translation only")
        payload_lines = [
            {
                "line_id": line.line_id,
                "text": line.text,
                "duration_ms": line.end_ms - line.start_ms,
            }
            for line in transcript.lines
        ]
        prompt = {
            "task": "subtitle_translation",
            "source_language": request.source_language,
            "target_language": request.target_language,
            "character_names": request.character_names,
            "glossary": request.glossary,
            "rules": [
                "Preserve every line_id exactly once and in the same order.",
                "Do not merge or split subtitle lines.",
                "Use natural spoken language suitable for dubbing.",
                "Keep wording concise enough for the supplied duration_ms.",
                'Return JSON only: {"lines":[{"line_id":1,"text":"..."}]}',
            ],
            "lines": payload_lines,
        }
        parsed = await self._post_json(
            self._body(
                prompt=prompt,
                temperature=0.25,
                system="You are a precise multilingual subtitle localization engine.",
            ),
            timeout=25.0,
        )
        translated_rows = parsed.get("lines")
        if not isinstance(translated_rows, list):
            raise ValueError("DeepSeek response is missing a lines array")
        parsed_ids = [int(item["line_id"]) for item in translated_rows]
        expected_ids = [line.line_id for line in transcript.lines]
        if parsed_ids != expected_ids:
            raise ValueError("DeepSeek response changed subtitle line IDs or ordering")
        translated_by_id = {
            int(item["line_id"]): str(item["text"]).strip() for item in translated_rows
        }
        if any(not translated_by_id[line_id] for line_id in expected_ids):
            raise ValueError("DeepSeek returned an empty subtitle line")
        return Transcript(
            language=request.target_language,
            lines=[
                SubtitleLine(
                    line_id=line.line_id,
                    start_ms=line.start_ms,
                    end_ms=line.end_ms,
                    text=translated_by_id[line.line_id],
                )
                for line in transcript.lines
            ],
        )

    async def reason_speakers(
        self,
        request: JobRequest,
        transcript: Transcript,
        evidence: list[LineEvidence],
        ambiguous_line_ids: set[int],
    ) -> list[LineEvidence]:
        """Return soft text evidence only for ambiguous lines.

        The model may rank existing candidates but is forbidden to invent a new
        character ID. Audio and visual evidence remain authoritative.
        """

        if not ambiguous_line_ids:
            return []
        by_line = {row.line_id: row for row in evidence}
        line_index = {line.line_id: index for index, line in enumerate(transcript.lines)}
        rows = []
        allowed_by_line: dict[int, set[str]] = {}
        for line_id in sorted(ambiguous_line_ids):
            index = line_index[line_id]
            line = transcript.lines[index]
            row = by_line.get(line_id, LineEvidence(line_id=line_id))
            candidates = {
                item.character_id for item in [*row.audio, *row.visual] if item.character_id
            }
            if not candidates:
                continue
            allowed_by_line[line_id] = candidates
            context = transcript.lines[max(0, index - 2) : index + 3]
            rows.append(
                {
                    "line_id": line_id,
                    "text": line.text,
                    "context": [{"line_id": item.line_id, "text": item.text} for item in context],
                    "candidates": sorted(candidates),
                    "audio": [item.model_dump() for item in row.audio],
                    "visual": [item.model_dump() for item in row.visual],
                    "offscreen": row.offscreen,
                    "character_names": request.character_names,
                }
            )
        if not rows:
            return []

        prompt = {
            "task": "speaker_attribution_soft_evidence",
            "rules": [
                "Output JSON only.",
                "Return every supplied line_id exactly once and in order.",
                "Rank only candidate IDs supplied for that line; never invent IDs.",
                "Scores must be between 0 and 1 and should sum approximately to 1.",
                "Treat this as weak dialogue-context evidence, not certainty.",
            ],
            "output_schema": {
                "lines": [
                    {
                        "line_id": 1,
                        "candidates": [{"character_id": "character_001", "score": 0.7}],
                    }
                ]
            },
            "lines": rows,
        }
        parsed = await self._post_json(
            self._body(
                prompt=prompt,
                temperature=0.1,
                system=(
                    "You provide conservative JSON dialogue-context evidence for "
                    "speaker attribution in film subtitles."
                ),
            ),
            timeout=12.0,
        )
        output = parsed.get("lines")
        expected_ids = [row["line_id"] for row in rows]
        output_ids = [int(item["line_id"]) for item in output] if isinstance(output, list) else []
        if output_ids != expected_ids:
            raise ValueError("DeepSeek speaker reasoning changed line IDs or ordering")

        result: list[LineEvidence] = []
        for item in output:
            line_id = int(item["line_id"])
            allowed = allowed_by_line[line_id]
            candidates = []
            for candidate in item.get("candidates", []):
                character_id = str(candidate["character_id"])
                if character_id not in allowed:
                    raise ValueError("DeepSeek invented a speaker candidate")
                candidates.append(
                    CandidateScore(character_id=character_id, score=float(candidate["score"]))
                )
            result.append(LineEvidence(line_id=line_id, text=candidates))
        return result
