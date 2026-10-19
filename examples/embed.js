// Import this from your customer's website. Keep API keys exclusively on its server.
export async function startVoice({ platformUrl, botId, tokenEndpoint, audioElement }) {
  const tokenResponse = await fetch(tokenEndpoint, { method: "POST" });
  if (!tokenResponse.ok) throw new Error("Could not obtain a voice token");
  const { token } = await tokenResponse.json();
  const pc = new RTCPeerConnection({ iceServers: [] }); // Configure production TURN here.
  let stream;
  let pcId;
  const base = `${platformUrl}/api/v1/public/bots/${botId}/webrtc`;
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const cleanup = () => {
    stream?.getTracks().forEach(track => track.stop());
    pc.close();
    audioElement.srcObject = null;
  };
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach(track => pc.addTrack(track, stream));
    pc.createDataChannel("pipecat");
    pc.ontrack = event => {
      audioElement.srcObject = new MediaStream([event.track]);
      audioElement.play().catch(() => {}); // Show audio controls if autoplay is blocked.
    };
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise((resolve, reject) => {
      if (pc.iceGatheringState === "complete") return resolve();
      const timeout = setTimeout(() => {
        pc.removeEventListener("icegatheringstatechange", onChange);
        reject(new Error("ICE gathering timed out"));
      }, 15000);
      function onChange() {
        if (pc.iceGatheringState === "complete") {
          clearTimeout(timeout);
          pc.removeEventListener("icegatheringstatechange", onChange);
          resolve();
        }
      }
      pc.addEventListener("icegatheringstatechange", onChange);
    });
    const response = await fetch(`${base}/offer`, {
      method: "POST", headers, body: JSON.stringify(pc.localDescription)
    });
    if (!response.ok) throw new Error(`Voice negotiation failed (${response.status})`);
    const answer = await response.json();
    pcId = answer.pc_id;
    await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    return async function stop() {
      cleanup();
      await fetch(`${base}/${pcId}`, { method: "DELETE", headers });
    };
  } catch (error) {
    cleanup();
    if (pcId) await fetch(`${base}/${pcId}`, { method: "DELETE", headers }).catch(() => {});
    throw error;
  }
}
