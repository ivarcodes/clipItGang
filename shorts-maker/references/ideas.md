# Extending shorts-maker

Things that would make it better, in rough order of payoff:

1. **Face-tracked crop**: sample frames with MediaPipe/OpenCV face detection, smooth the x positions, and feed a time-varying crop expression to ffmpeg. Replaces the fixed `crop_x`.
2. **Audio-energy / laughter detection**: for videos where the transcript is thin, find loud or laughter segments with librosa or ffmpeg `astats`, and feed those timestamps to the clip picker.
3. **Vision pass**: extract a frame every few seconds and send to a vision-capable model to find visual moments (reactions, demos).
4. **Silence trimming**: use ffmpeg `silencedetect` to cut dead air inside a clip.
5. **B-roll / zoom punch-ins**: add slow zooms on emphasis words.
6. **Metadata generation**: titles, descriptions, hashtags per clip.
7. **Batch/queue**: wrap `render_clips.py` in a job queue (RQ/Celery) behind a small web UI with start/end sliders for human review.
