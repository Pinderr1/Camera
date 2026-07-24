// Camera + MediaPipe PoseLandmarker (browser Tasks API) + the JS port of
// extract_features() from src/senior_safety/pose_extractor.py.

const TASKS_VISION_VERSION = "0.10.14";
const CDN_BASE = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${TASKS_VISION_VERSION}`;
const MODEL_URLS = {
  lite: "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task",
  full: "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task",
};

export const VISIBILITY_MIN = 0.4;
const LEFT_SHOULDER = 11;
const RIGHT_SHOULDER = 12;
const LEFT_HIP = 23;
const RIGHT_HIP = 24;
const CORE_LANDMARKS = [LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP];
const PRESENCE_MIN_CORE_LANDMARKS = 2;

export const SKELETON_CONNECTIONS = [
  [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
  [11, 23], [12, 24], [23, 24],
  [23, 25], [25, 27], [24, 26], [26, 28],
];

export async function openCamera(videoEl) {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: "environment", width: { ideal: 640 }, height: { ideal: 480 } },
    audio: false,
  });
  videoEl.srcObject = stream;
  await videoEl.play();
  return stream;
}

async function fetchModel(url, onProgress) {
  try {
    const cache = await caches.open("bedwatch-model");
    const cached = await cache.match(url);
    if (cached) return await cached.arrayBuffer();
    onProgress?.("Downloading pose model (~6 MB, first run only)...");
    const response = await fetch(url);
    await cache.put(url, response.clone());
    return await response.arrayBuffer();
  } catch {
    const response = await fetch(url);
    return await response.arrayBuffer();
  }
}

export async function createLandmarker(model, onProgress) {
  const { FilesetResolver, PoseLandmarker } = await import(
    `${CDN_BASE}/vision_bundle.mjs`
  );
  const fileset = await FilesetResolver.forVisionTasks(`${CDN_BASE}/wasm`);
  const buffer = await fetchModel(MODEL_URLS[model] || MODEL_URLS.lite, onProgress);
  // Options mirror the `pose` block in config/zones.example.json.
  return await PoseLandmarker.createFromOptions(fileset, {
    baseOptions: { modelAssetBuffer: new Uint8Array(buffer), delegate: "GPU" },
    runningMode: "VIDEO",
    numPoses: 1,
    minPoseDetectionConfidence: 0.3,
    minPosePresenceConfidence: 0.3,
    minTrackingConfidence: 0.5,
  });
}

export function extractFeatures(landmarks, timestampMs) {
  const empty = {
    timestampMs,
    personPresent: false,
    hip: null,
    shoulderMid: null,
    coreConfidence: 0,
    luma: null,
  };
  if (!landmarks) return empty;
  const vis = (i) => landmarks[i]?.visibility ?? 0;
  const coreVisible = CORE_LANDMARKS.filter((i) => vis(i) >= VISIBILITY_MIN);
  if (coreVisible.length < PRESENCE_MIN_CORE_LANDMARKS) return empty;

  const midpoint = (a, b) =>
    vis(a) >= VISIBILITY_MIN && vis(b) >= VISIBILITY_MIN
      ? [(landmarks[a].x + landmarks[b].x) / 2, (landmarks[a].y + landmarks[b].y) / 2]
      : null;

  return {
    timestampMs,
    personPresent: true,
    hip: midpoint(LEFT_HIP, RIGHT_HIP),
    shoulderMid: midpoint(LEFT_SHOULDER, RIGHT_SHOULDER),
    coreConfidence:
      coreVisible.reduce((sum, i) => sum + vis(i), 0) / coreVisible.length,
    luma: null,
  };
}

const lumaCanvas = typeof document !== "undefined" ? document.createElement("canvas") : null;

export function sampleLuma(videoEl) {
  if (!lumaCanvas || !videoEl.videoWidth) return null;
  lumaCanvas.width = 32;
  lumaCanvas.height = 24;
  const ctx = lumaCanvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(videoEl, 0, 0, 32, 24);
  const { data } = ctx.getImageData(0, 0, 32, 24);
  let total = 0;
  for (let i = 0; i < data.length; i += 4) {
    total += 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
  }
  return total / (data.length / 4);
}
