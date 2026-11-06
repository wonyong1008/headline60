"""Reuse an approved synthetic face texture in a draft; not identity recognition."""
import argparse
import hashlib
import json
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from PIL import Image


def landmarks(image):
    with mp.solutions.face_mesh.FaceMesh(static_image_mode=True,max_num_faces=1,
                                       refine_landmarks=True,min_detection_confidence=.5) as detector:
        result=detector.process(image)
    if not result.multi_face_landmarks:
        raise RuntimeError('No usable face was detected; no output created.')
    height,width=image.shape[:2]
    return np.array([(point.x*width,point.y*height) for point in result.multi_face_landmarks[0].landmark],dtype=np.float32)


def endpoints(connections):
    return sorted({point for edge in connections for point in edge})


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--reference',required=True,type=Path)
    parser.add_argument('--draft',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if args.output.exists() or args.output.resolve() in {args.reference.resolve(),args.draft.resolve()}:
        raise RuntimeError('Inputs and existing outputs must be preserved.')
    reference=np.array(Image.open(args.reference).convert('RGB'))
    draft=np.array(Image.open(args.draft).convert('RGB'))
    a,b=landmarks(reference),landmarks(draft)
    eye_sets=[endpoints(mp.solutions.face_mesh.FACEMESH_LEFT_EYE),endpoints(mp.solutions.face_mesh.FACEMESH_RIGHT_EYE)]
    source=np.array([a[indices].mean(axis=0) for indices in eye_sets])
    target=np.array([b[indices].mean(axis=0) for indices in eye_sets])
    source_vector=source[1]-source[0];target_vector=target[1]-target[0]
    scale=np.linalg.norm(target_vector)/np.linalg.norm(source_vector)
    theta=np.arctan2(target_vector[1],target_vector[0])-np.arctan2(source_vector[1],source_vector[0])
    rotation=np.array([[np.cos(theta),-np.sin(theta)],[np.sin(theta),np.cos(theta)]],dtype=np.float32)*scale
    translation=target.mean(axis=0)-rotation@source.mean(axis=0)
    matrix=np.column_stack((rotation,translation)).astype(np.float32)
    hull=cv2.convexHull(a[endpoints(mp.solutions.face_mesh.FACEMESH_FACE_OVAL)].astype(np.int32))
    mask=np.zeros(reference.shape[:2],dtype=np.uint8);cv2.fillConvexPoly(mask,hull,255)
    feather=max(4,np.ptp(hull[:,0,0])*.035)
    mask=np.clip((cv2.distanceTransform(mask,cv2.DIST_L2,5)-1)/feather,0,1)
    size=(draft.shape[1],draft.shape[0])
    warped=cv2.warpAffine(reference,matrix,size,flags=cv2.INTER_CUBIC)
    alpha=cv2.warpAffine(mask,matrix,size,flags=cv2.INTER_LINEAR)[...,None]
    result=np.clip(warped*alpha+draft*(1-alpha),0,255).astype(np.uint8)
    Image.fromarray(result).save(args.output)
    metadata={'method':'approved reference face texture, eye-aligned similarity transform and inward feather',
              'reference':str(args.reference),'reference_sha256':hashlib.sha256(args.reference.read_bytes()).hexdigest(),
              'raw_draft':str(args.draft),'output':str(args.output),'matrix':matrix.tolist(),
              'changed_pixels':int(np.any(result!=draft,axis=2).sum()),'biometric_verification':False}
    args.output.with_suffix('.json').write_text(json.dumps(metadata,indent=2))
    print(json.dumps(metadata))

if __name__=='__main__':main()
