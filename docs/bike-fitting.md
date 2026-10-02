# Local bike fitting video analysis

The experimental fitting workspace is available at `/bike-fitting` when
`VITE_ENABLE_BIKE_FITTING=true`. The feature flag defaults to false. It adds no
server endpoint and sends neither a selected video nor pose coordinates to a
server. Keep the flag disabled in production until the product owner enables it.

The browser accepts one local side-view video of stationary road or gravel
cycling, with the rider's full body visible and hands on the hoods. A 10–30
second clip is recommended. Files over 200 MB and videos longer than 60 seconds
are rejected. Video decoding and pose inference use browser APIs on the device.
Frames are resized so their longest edge is at most 720 pixels and are sent one
at a time to a Web Worker using transferable `ImageBitmap` objects. The
worker uses MediaPipe Tasks Vision in `VIDEO` mode with monotonically increasing
timestamps. The UI reports 2D joint angles and tracking confidence only; it
does not grade fit or recommend changes.

The measurements use the decoded image's pixel aspect ratio. Knee, hip, elbow,
and ankle values are 2D included angles between adjacent body segments; ankle
uses the shin and the heel-to-foot-index axis through the ankle vertex.
Torso inclination is 0° when horizontal and 90° when upright. These are
image-plane measurements and are not corrected for camera perspective or
reported as three-dimensional joint angles.

## Model and WASM assets

`pnpm dev` and `pnpm build` run
`frontend/scripts/prepare-bike-fitting-assets.mjs`. It copies the six WASM
loader/binary files from the pinned `@mediapipe/tasks-vision` 1.0.1 package to
`frontend/public/bike-fitting-assets/mediapipe-1.0.1/wasm/`. It downloads the
MediaPipe Pose Landmarker Lite float16 v1 model into the ignored
`frontend/public/bike-fitting-assets/pose_landmarker_lite-v1.task` only when a
verified local copy is not present. The script checks the expected 5,777,746
byte size and SHA-256 digest
`59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a`, limits
redirects and download size, and uses a 20 second request timeout. Build and
development fail if the package or model cannot be verified.

The browser requests these same-origin assets only when analysis is started.
The inference worker response also carries a restrictive Content Security
Policy with `connect-src 'self'`. Static hosts other than Cloudflare Pages must
apply the same policy to the emitted `pose.worker-*.js` worker asset; otherwise
MediaPipe's internal telemetry can attempt a cross-origin POST.
The MediaPipe Tasks Vision package and model are Apache-2.0. The complete
upstream license is included as `frontend/third-party/mediapipe-LICENSE.txt`
and copied to `bike-fitting-assets/LICENSE.txt` in the built site. It is from
MediaPipe revision
[`b453bf83fed4eb220051b3d45bf0b54cadbb7910`](https://github.com/google-ai-edge/mediapipe/blob/b453bf83fed4eb220051b3d45bf0b54cadbb7910/LICENSE).
The model source is Google's [Pose Landmarker Lite float16 v1
artifact](https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task),
documented by the [MediaPipe Pose Landmarker guide](https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker).
The model and package are pinned to make the asset source and bytes
reproducible. Their upstream notices remain applicable.

## Verification status

The browser test's 1-second WebM is a synthetic blank fixture. Its JPEG frame is
rendered in Playwright from a 720×400 canvas with a blue-gray background and the
text “Blank fixture: no cyclist”, duplicated 20 times, then muxed with
Playwright's bundled FFmpeg using:

```sh
ffmpeg -y -f image2pipe -framerate 20 -c:v mjpeg -i pipe:0 \
  -c:v libvpx -f webm issue-192-blank-1s.webm
```

It contains no rider footage and only verifies browser decode and worker
initialization/inference.

Automated geometry tests can verify synthetic coordinates and input limits.
They do not establish measurement accuracy for real riders. Physical-device
testing, browser compatibility checks on representative phones, and comparison
with manually annotated reference cycling videos remain pending. No accuracy
claim is made.
