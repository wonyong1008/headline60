"""One model per subprocess; fixed preset voices, offline inference, no cloning."""
import argparse
import json
import os
import resource
import time
from pathlib import Path


def main():
    os.environ['HF_HUB_OFFLINE']='1'
    os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    parser=argparse.ArgumentParser()
    parser.add_argument('request',type=Path)
    args=parser.parse_args()
    payload=json.loads(args.request.read_text())
    started=time.perf_counter()
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model
    
    mx.set_memory_limit(9 * 1024**3)
    model=load_model(payload['model_path'])
    load_seconds=time.perf_counter()-started
    results=[]
    for item in payload['items']:
        mx.random.seed(payload.get('seed',42))
        begin=time.perf_counter()
        audio=[]
        for result in model.generate_custom_voice(text=item['text'], speaker=payload.get('speaker','Sohee'),
                language='Korean', instruct=item.get('style_prompt',payload.get('style_prompt','Read as a calm, clear Korean news anchor.')),
                temperature=payload.get('temperature',0.9), top_p=payload.get('top_p',1.0),
                top_k=payload.get('top_k',50), repetition_penalty=payload.get('repetition_penalty',1.05),
                max_tokens=payload.get('max_tokens',1200), verbose=False):
            mx.eval(result.audio)
            audio.append(np.asarray(result.audio))
            sample_rate=result.sample_rate
        if not audio:
            raise RuntimeError('TTS가 음성을 반환하지 않았습니다.')
        if result.token_count >= payload.get('max_tokens',1200):
            raise RuntimeError(f'TTS 토큰 한도 도달: {result.token_count}. 잘린 음성을 성공 처리하지 않습니다.')
        waveform=np.concatenate(audio)
        if waveform.ndim>1:
            waveform=waveform.reshape(-1)
        path=Path(item['output'])
        path.parent.mkdir(parents=True,exist_ok=True)
        sf.write(path,waveform,sample_rate,subtype='PCM_16')
        seconds=len(waveform)/sample_rate
        elapsed=time.perf_counter()-begin
        record={'id':item['id'],'path':str(path),'duration':seconds,'generate_seconds':elapsed,'rtf':elapsed/seconds,'token_count':result.token_count}
        results.append(record)
        print(json.dumps({'event':'tts_item',**record}),flush=True)
    metrics={'load_seconds':load_seconds,'items':results,'total_seconds':time.perf_counter()-started,
             'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
             'mlx_peak_memory_bytes':mx.get_peak_memory(),'speaker':payload.get('speaker','Sohee'),
             'model_path':payload['model_path']}
    metrics['generation_parameters']={k:payload.get(k,v) for k,v in {'temperature':.9,'top_p':1.,'top_k':50,'repetition_penalty':1.05,'seed':42}.items()}
    Path(payload['metrics_path']).write_text(json.dumps(metrics,indent=2,ensure_ascii=False))
    print(json.dumps(metrics,ensure_ascii=False),flush=True)


if __name__ == "__main__":
    main()
