// Typed client for the FastAPI backend. All calls go through /api which the
// Next.js dev server proxies to http://localhost:8000 (see next.config.mjs).

export interface Citation {
  index: number;
  source_file: string;
  source_path: string | null;
  location: string;
  file_type: string | null;
  matched_by: string[];
  rrf_score: number | null;
}

export interface SearchHit {
  id: string;
  text: string;
  metadata: Record<string, any>;
  rrf_score: number;
  semantic_score: number | null;
  keyword_score: number | null;
  matched_by: string[];
}

export interface Stats {
  total_chunks: number;
  sources: Record<string, number>;
  supported_extensions: string[];
}

export interface Health {
  status: string;
  models: {
    ollama_reachable: boolean;
    llm_model: string;
    llm_ready: boolean;
    embed_model: string;
    embed_ready: boolean;
    installed_models: string[];
  };
}

export async function getHealth(): Promise<Health> {
  const r = await fetch("/api/health");
  if (!r.ok) throw new Error("health check failed");
  return r.json();
}

export async function getStats(): Promise<Stats> {
  const r = await fetch("/api/stats");
  if (!r.ok) throw new Error("stats failed");
  return r.json();
}

export async function search(query: string): Promise<{ results: SearchHit[] }> {
  const r = await fetch("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!r.ok) throw new Error("search failed");
  return r.json();
}

export async function uploadFiles(files: FileList): Promise<{ saved: string[]; skipped: any[] }> {
  const form = new FormData();
  Array.from(files).forEach((f) => form.append("files", f));
  const r = await fetch("/api/upload", { method: "POST", body: form });
  if (!r.ok) throw new Error("upload failed");
  return r.json();
}

export interface IndexStatus {
  status: "idle" | "pending" | "running" | "done" | "error";
  total_files?: number;
  processed_files?: number;
  current_file?: string | null;
  total_chunks?: number;
  file_chunk_done?: number;
  file_chunk_total?: number;
  message?: string;
  error?: string | null;
  elapsed_s?: number;
}

export async function startIndex(): Promise<{ job_id: string; status: string }> {
  const r = await fetch("/api/index", { method: "POST" });
  if (!r.ok) throw new Error("failed to start indexing");
  return r.json();
}

export async function getIndexStatus(jobId?: string): Promise<IndexStatus> {
  const url = jobId ? `/api/index/status?job_id=${jobId}` : "/api/index/status";
  const r = await fetch(url);
  if (!r.ok) throw new Error("status check failed");
  return r.json();
}

// Starts indexing and polls until done/error, reporting progress via onProgress.
export async function indexAll(
  onProgress?: (s: IndexStatus) => void
): Promise<IndexStatus> {
  const { job_id } = await startIndex();
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        const s = await getIndexStatus(job_id);
        onProgress?.(s);
        if (s.status === "done") resolve(s);
        else if (s.status === "error") reject(new Error(s.error || "indexing error"));
        else setTimeout(poll, 1000);
      } catch (e) {
        reject(e);
      }
    };
    poll();
  });
}

// Streams the RAG answer. Calls onCitations once, then onToken for each delta.
export async function ask(
  question: string,
  onCitations: (c: Citation[]) => void,
  onToken: (t: string) => void
): Promise<void> {
  const r = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!r.ok || !r.body) throw new Error("ask failed");

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const msg = JSON.parse(line);
      if (msg.type === "citations") onCitations(msg.data as Citation[]);
      else if (msg.type === "token") onToken(msg.data as string);
    }
  }
}
