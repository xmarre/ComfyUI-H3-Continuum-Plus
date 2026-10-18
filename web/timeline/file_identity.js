// The File API does not expose the source path. Hash bounded slices instead
// of uploading movie bytes or allocating a whole-film ArrayBuffer.
export const CHUNK_BYTES = 16 * 1024 * 1024;
export const MAX_BYTES = 128 * 1024 ** 3;

export async function fingerprintFile(file, { signal, progress } = {}) {
  if (!file || !Number.isSafeInteger(file.size) || file.size <= 0 ||
      file.size > MAX_BYTES || typeof file.name !== "string" ||
      !file.name || file.name.includes("/") || file.name.includes("\\")) {
    throw new Error("Unsupported dropped file name or size (max 128 GiB)");
  }
  if (!globalThis.crypto?.subtle) {
    throw new Error("Local file identity requires a secure browser context (HTTPS or localhost)");
  }
  const chunks = [];
  for (let offset = 0; offset < file.size; offset += CHUNK_BYTES) {
    if (signal?.aborted) throw signal.reason ?? new Error("Import cancelled");
    // A single chunk is the upper bound of live browser-side movie data.
    const buffer = await file.slice(
      offset, Math.min(offset + CHUNK_BYTES, file.size),
    ).arrayBuffer();
    if (signal?.aborted) throw signal.reason ?? new Error("Import cancelled");
    const hash = await globalThis.crypto.subtle.digest("SHA-256", buffer);
    chunks.push(
      Array.from(new Uint8Array(hash), (byte) =>
        byte.toString(16).padStart(2, "0"),
      ).join(""),
    );
    progress?.(Math.min(1, (offset + buffer.byteLength) / file.size));
  }
  return { name: file.name, size: file.size, chunk_size: CHUNK_BYTES, chunks };
}
