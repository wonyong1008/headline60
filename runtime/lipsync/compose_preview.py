"""Composite only synthesized mouth pixels onto an approved static presenter image."""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--folder',required=True,type=Path)
    args=parser.parse_args();folder=args.folder.resolve()
    source=json.loads((folder/'source.json').read_text())
    lip=json.loads((folder/'lip-metrics.json').read_text())
    if lip.get('status')!='completed':raise RuntimeError('Lip inference did not complete.')
    output=folder/'output.mp4'
    if output.exists():raise RuntimeError('Previous preview must not be overwritten.')
    base=cv2.imread(source['approved_image'])
    crop=cv2.imread(str(folder/'portrait-crop.png'))
    with mp.solutions.face_mesh.FaceMesh(static_image_mode=True,max_num_faces=1,
                                       refine_landmarks=True,min_detection_confidence=.5) as detector:
        faces=detector.process(cv2.cvtColor(crop,cv2.COLOR_BGR2RGB)).multi_face_landmarks
    if not faces:raise RuntimeError('No mouth landmarks found.')
    points=np.array([(p.x*512,p.y*512) for p in faces[0].landmark])
    indices=sorted({i for edge in mp.solutions.face_mesh.FACEMESH_LIPS for i in edge})
    lips=points[indices];center=lips.mean(axis=0);width=np.ptp(lips[:,0]);height=np.ptp(lips[:,1])
    mask=np.zeros((512,512),dtype=np.uint8)
    axes=(round(width*.86),round(max(height*2.0,width*.52)))
    cv2.ellipse(mask,tuple(np.round(center).astype(int)),axes,0,0,360,255,-1)
    mask=cv2.GaussianBlur(mask,(0,0),sigmaX=7)
    left,top,right,bottom=source['crop_box'];size=(right-left,bottom-top)
    alpha=(cv2.resize(mask,size,interpolation=cv2.INTER_LINEAR)/255)[...,None]
    capture=cv2.VideoCapture(str(folder/'mouth.mp4'))
    fps=float(capture.get(cv2.CAP_PROP_FPS));frames=0
    base_roi=base[top:bottom,left:right].astype(np.float32)
    with tempfile.TemporaryDirectory(prefix='headline60-preview-') as temporary:
        silent=Path(temporary)/'silent.mp4'
        with (folder/'compose-ffmpeg.log').open('w') as log:
            encoder=subprocess.Popen(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','bgr24',
                '-s',f'{base.shape[1]}x{base.shape[0]}','-r',str(fps),'-i','pipe:0','-an',
                '-vf','fps=30','-c:v','h264_videotoolbox','-allow_sw','0','-b:v','5M',
                '-pix_fmt','yuv420p',str(silent)],stdin=subprocess.PIPE,stderr=log)
            try:
                while True:
                    ok,frame=capture.read()
                    if not ok:break
                    patch=cv2.resize(frame,size,interpolation=cv2.INTER_AREA)
                    final=base.copy()
                    final[top:bottom,left:right]=np.clip(patch*alpha+base_roi*(1-alpha),0,255).astype(np.uint8)
                    encoder.stdin.write(final.tobytes());frames+=1
                encoder.stdin.close()
                if encoder.wait(timeout=60):raise RuntimeError('Preview encoder failed.')
            finally:
                capture.release()
                if encoder.poll() is None:encoder.kill();encoder.wait()
        if frames!=125:raise RuntimeError(f'Expected 125 mouth frames, got {frames}.')
        partial=folder/'preview.partial.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(silent),'-i',str(folder/'input.wav'),
            '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','160k','-t','5',
            '-movflags','+faststart',str(partial)],check=True)
        info=json.loads(subprocess.check_output(['ffprobe','-v','error','-count_frames','-show_streams',
            '-show_format','-of','json',str(partial)]))
        video=next(s for s in info['streams'] if s['codec_type']=='video')
        audio=next(s for s in info['streams'] if s['codec_type']=='audio')
        if (video['width'],video['height'],video['avg_frame_rate'],video['nb_read_frames'])!=(720,1280,'30/1','150'):
            raise RuntimeError('Preview dimensions/frame count are incorrect.')
        if audio['codec_name']!='aac' or abs(float(info['format']['duration'])-5)>.04:
            raise RuntimeError('Preview audio/duration are incorrect.')
        subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(partial),'-f','null','-'],check=True)
        partial.rename(output)
    (folder/'preview-ffprobe.json').write_text(json.dumps(info,indent=2))
    metadata={**lip,'source_kind':'approved_outfit_static_mouth_composite','width':720,'height':1280,
              'fps':30,'rendered_frames':150,'output_bytes':output.stat().st_size,'output_path':str(output),
              'approved_source_image':source['approved_image'],'body_motion_generated':False,
              'static_portrait_mouth_only':True,'identity_preservation':'Approved still reused; only feathered mouth ROI changes.',
              'mouth_mask':{'center':center.tolist(),'axes':axes,'crop_box':source['crop_box']},
              'lipsync_quality_review':'awaiting_user_review','benchmark_scope':'lip worker timing excludes this final composition'}
    (folder/'metrics.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    print(output)

if __name__=='__main__':main()
