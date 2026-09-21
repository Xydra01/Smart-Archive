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

// Optional query scope. Selecting sources sends `sources`; choosing a group
// (without an ad-hoc selection) sends `group_id`. When neither is set, the
// client sends neither key so the query runs against the whole archive.
export interface QueryScopeArg {
  sources?: string[];
  group_id?: string;
}

// Merges a scope into a request body, only including keys that are meaningfully
// set so an unset scope sends neither (whole archive per Req 15.4).
function scopeBody(scope?: QueryScopeArg): Record<string, unknown> {
  return {
    ...(scope?.sources?.length ? { sources: scope.sources } : {}),
    ...(scope?.group_id ? { group_id: scope.group_id } : {}),
  };
}

export async function search(
  query: string,
  scope?: QueryScopeArg
): Promise<{ results: SearchHit[] }> {
  const r = await fetch("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, ...scopeBody(scope) }),
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
  },
  scope?: QueryScopeArg
): Promise<void> {
  const r = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, ...scopeBody(scope) }),
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

// ---------------------------------------------------------------------------
// Source selection and groups
// ---------------------------------------------------------------------------

// A named group of sources. `members` are source_path values; `present` (when
// returned) flags whether each member is currently in the Vector_Store.
export interface Group {
  group_id: string;
  name: string;
  members: string[];
  present?: Record<string, boolean>;
}

// A source that can be selected for scoping, with its chunk count.
export interface SelectableSource {
  source_path: string;
  chunks: number;
}

export async function getSelectableSources(): Promise<{ sources: SelectableSource[] }> {
  const r = await fetch("/api/selectable-sources");
  if (!r.ok) throw new Error("failed to load selectable sources");
  return r.json();
}

export async function listGroups(): Promise<{ groups: Group[] }> {
  const r = await fetch("/api/groups");
  if (!r.ok) throw new Error("failed to list groups");
  return r.json();
}

export async function createGroup(name: string): Promise<Group> {
  const r = await fetch("/api/groups", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || "failed to create group");
  }
  return r.json();
}

export async function renameGroup(id: string, name: string): Promise<Group> {
  const r = await fetch(`/api/groups/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || "failed to rename group");
  }
  return r.json();
}

export async function deleteGroup(id: string): Promise<void> {
  const r = await fetch(`/api/groups/${id}`, { method: "DELETE" });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || "failed to delete group");
  }
}

export async function addSources(id: string, sources: string[]): Promise<Group> {
  const r = await fetch(`/api/groups/${id}/sources`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sources }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || "failed to add sources");
  }
  return r.json();
}

export async function removeSources(id: string, sources: string[]): Promise<Group> {
  const r = await fetch(`/api/groups/${id}/sources`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sources }),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new Error(detail.detail || "failed to remove sources");
  }
  return r.json();
}
