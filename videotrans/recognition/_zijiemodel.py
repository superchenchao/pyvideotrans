import json
from dataclasses import dataclass
from pathlib import Path
from typing import List,  Union

from videotrans.configure.excepts import SpeechToTextError
from videotrans.recognition._base import BaseRecogn
from videotrans.recognition.volcengine_flash import (
    extract_utterances,
    request_transcription,
)
from videotrans.task.taskcfg import SrtItem
from videotrans.util import tools


@dataclass
class ZijieRecogn(BaseRecogn):

    def _exec(self) -> Union[List[SrtItem], None]:
        if self._exit():  return
        res, trace_id = request_transcription(self.audio_file)
        seg_list = extract_utterances(res)
        if not seg_list:
            raise SpeechToTextError('火山极速版返回数据中无识别结果')

        srt_list = []
        speaker_list = []
        srt_strings = ""

        for it in seg_list:
            if not it.get('text', '').strip():
                continue
            speaker_list.append(it['speaker'])
            startraw = tools.ms_to_time_string(ms=it['start_time'])
            endraw = tools.ms_to_time_string(ms=it['end_time'])
            tmp = SrtItem(
                line=len(srt_list) + 1,
                start_time=it['start_time'],
                end_time=it['end_time'],
                startraw=startraw,
                endraw=endraw,
                text=it['text'].strip()
            )
            srt_list.append(tmp)
            srt_strings += f"{tmp['line']}\n{startraw} --> {endraw}\n{tmp['text']}\n\n"

        self.signal(
            text=srt_strings,
            type='replace_subtitle'
        )
        if speaker_list:
            Path(f'{self.cache_folder}/speaker.json').write_text(json.dumps(speaker_list), encoding='utf-8')
            Path(f'{self.cache_folder}/speaker.volcengine.json').write_text(
                json.dumps({
                    "provider": "volcengine_flash",
                    "trace_id": trace_id,
                    "speaker_count": len(set(speaker_list)),
                }, ensure_ascii=False),
                encoding='utf-8',
            )
        return srt_list
