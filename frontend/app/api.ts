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
  indexed_files: number;
  manifest: Record<string, { chunks: number; size: number }>;
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
  skipped_files?: number;
  removed_files?: number;
  current_file?: string | null;
  total_chunks?: number;
  file_chunk_done?: number;
  file_chunk_total?: number;
  message?: string;
  error?: string | null;
  elapsed_s?: number;
}

export async function startIndex(force = false): Promise<{ job_id: string; status: string }> {
  const r = await fetch(`/api/index?force=${force}`, { method: "POST" });
  if (!r.ok) throw new Error("failed to start indexing");
  return r.json();
}

export async function importFolder(
  folder: string,
  hardlink = false
): Promise<{ found: number; imported: number; skipped: number; into: string }> {
  const r = await fetch("/api/import-folder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folder, hardlink }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || "folder import failed");
  }
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
  onProgress?: (s: IndexStatus) => void,
  force = false
): Promise<IndexStatus> {
  const { job_id } = await startIndex(force);
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

export type AnswerSection = "summary" | "per_source";

// Streams the RAG answer. Emits citations once, then a section marker before
// each block of tokens (summary first, then per-source findings).
export async function ask(
  question: string,
  handlers: {
    onCitations: (c: Citation[]) => void;
    onSection: (s: AnswerSection) => void;
    onToken: (section: AnswerSection, t: string) => void;
  }
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
  let current: AnswerSection = "summary";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      const msg = JSON.parse(line);
      if (msg.type === "citations") {
        handlers.onCitations(msg.data as Citation[]);
      } else if (msg.type === "section") {
        current = msg.data as AnswerSection;
        handlers.onSection(current);
      } else if (msg.type === "token") {
        handlers.onToken(current, msg.data as string);
      }
    }
  }
}
