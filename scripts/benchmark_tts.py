"""Explicit local speech benchmark; example quantities are not news claims."""
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from app.config import settings
from app.jobs.pipeline import exclusive
from app.process import run, save_json
from app.script.models import Scene
from app.tts.engine import SpeechDocument, join_wavs, synthesize
from app.tts.profiles import get

parser=argparse.ArgumentParser()
parser.add_argument('--profile',required=True)
parser.add_argument('--seconds',type=int,choices=[30,60],default=60)
args=parser.parse_args()
texts=[
'지금부터 헤드라인 육십의 한국어 음성 품질을 점검합니다.',
'2026년 10월 28일은 날짜 발음을 확인하기 위한 예시입니다.',
'예시 금액은 5억 3,000만 원이며, 실제 거래 금액을 뜻하지 않습니다.',
'3.5%는 비율을 읽는 방식을 점검하기 위한 테스트 수치입니다.',
'1,250건은 숫자와 단위의 연결을 확인하는 예시입니다.',
'자동차 모델 EV6와 영문 약어 BMW는 발음 사전을 바탕으로 읽습니다.',
'위고비와 마운자로는 의약품 이름의 발음만 확인합니다.',
'이 샘플은 의약품의 효과나 치료 방법을 설명하지 않습니다.',
'낯선 지역명과 아파트 이름은 제작자가 직접 발음을 확인합니다.',
'문장 사이에는 자연스러운 호흡을 두고, 문장 끝은 차분하게 마무리합니다.',
'같은 화자 설정을 유지하면서 여러 구간의 음량을 일정하게 맞춥니다.',
'음성 점검을 마칩니다. 실제 게시 전에는 전체 내용을 직접 들어 주세요.',
]
if args.seconds==30:texts=texts[:6]
folder=settings.data/'benchmarks'/('tts-'+args.profile+'-'+str(args.seconds)+'-'+datetime.now().strftime('%Y%m%dT%H%M%S'))
folder.mkdir(parents=True)
scenes=[Scene(headline='발음 점검',narration=text) for text in texts]
save_json(folder/'input.json',{'content_type':'pronunciation_benchmark_not_news','target_seconds':args.seconds,'scenes':[s.model_dump() for s in scenes]})
with exclusive(settings):
    lengths=synthesize(SpeechDocument(scenes),folder,settings,get(settings,args.profile))
    join_wavs([folder/f'speech-{i:02}.wav' for i in range(len(scenes))],folder/'raw-full.wav')
    tempo=sum(lengths)/args.seconds
    if not .5<=tempo<=2:raise RuntimeError(f'벤치마크 대본과 길이가 맞지 않습니다: 배속 {tempo:.2f}')
    run(['ffmpeg','-v','error','-y','-i',str(folder/'raw-full.wav'),'-af',f'atempo={tempo},apad,atrim=duration={args.seconds}',str(folder/'sample.wav')])
print(folder)
print((folder/'tts-metrics.json').read_text())
