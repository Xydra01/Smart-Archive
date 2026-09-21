"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  addSources,
  ask,
  Citation,
  createGroup,
  deleteGroup,
  getHealth,
  getSelectableSources,
  getStats,
  Group,
  Health,
  importFolder,
  indexAll,
  IndexStatus,
  listGroups,
  QueryScopeArg,
  removeSources,
  renameGroup,
  search,
  SearchHit,
  SelectableSource,
  Stats,
  uploadFiles,
} from "./api";

type Mode = "ask" | "search";
type ScopeMode = "archive" | "sources" | "group";

export default function Home() {
  const [mode, setMode] = useState<Mode>("ask");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);

  const [summary, setSummary] = useState("");
  const [perSource, setPerSource] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [results, setResults] = useState<SearchHit[]>([]);
  const [streaming, setStreaming] = useState(false);

  const [stats, setStats] = useState<Stats | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [notice, setNotice] = useState<string>("");
  const [indexing, setIndexing] = useState(false);
  const [folderPath, setFolderPath] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);

  // Scope + groups state
  const [selectable, setSelectable] = useState<SelectableSource[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [scopeMode, setScopeMode] = useState<ScopeMode>("archive");
  const [selectedSources, setSelectedSources] = useState<string[]>([]);
  const [selectedGroupId, setSelectedGroupId] = useState<string>("");

  // Groups manager UI state
  const [managerOpen, setManagerOpen] = useState(false);
  const [newGroupName, setNewGroupName] = useState("");
  const [editGroupId, setEditGroupId] = useState<string>("");

  async function refreshStatus() {
    try {
      setStats(await getStats());
      setHealth(await getHealth());
    } catch {
      /* backend may not be up yet */
    }
    // Scope data is refreshed alongside status so the source list and group
    // memberships stay current after any index run or group mutation.
    refreshScope();
  }

  async function refreshScope() {
    try {
      const [s, g] = await Promise.all([getSelectableSources(), listGroups()]);
      setSelectable(s.sources);
      setGroups(g.groups);
    } catch {
      /* backend may not be up yet */
    }
  }

  useEffect(() => {
    refreshStatus();
  }, []);

  // Drop any selected sources that no longer exist in the archive so the scope
  // never points at stale paths after a re-index or removal.
  useEffect(() => {
    const present = new Set(selectable.map((s) => s.source_path));
    setSelectedSources((prev) => prev.filter((p) => present.has(p)));
  }, [selectable]);

  // Clear the group selection if the chosen group was deleted.
  useEffect(() => {
    if (selectedGroupId && !groups.some((g) => g.group_id === selectedGroupId)) {
      setSelectedGroupId("");
    }
  }, [groups, selectedGroupId]);

  // Resolves the scope for a query. Ad-hoc selected sources take precedence
  // over a chosen group (matches backend resolution). Whole-archive sends
  // nothing so the client omits both keys.
  function resolveScope(): QueryScopeArg | undefined {
    if (scopeMode === "sources" && selectedSources.length > 0) {
      return { sources: selectedSources };
    }
    if (scopeMode === "group" && selectedGroupId) {
      return { group_id: selectedGroupId };
    }
    return undefined;
  }

  const activeScopeLabel = useMemo(() => {
    if (scopeMode === "sources" && selectedSources.length > 0) {
      return `Selected sources (${selectedSources.length})`;
    }
    if (scopeMode === "group" && selectedGroupId) {
      const g = groups.find((x) => x.group_id === selectedGroupId);
      if (g) return `${g.name} (${g.members.length} sources)`;
    }
    return "Whole archive";
  }, [scopeMode, selectedSources, selectedGroupId, groups]);

  function toggleSource(path: string) {
    setSelectedSources((prev) =>
      prev.includes(path) ? prev.filter((p) => p !== path) : [...prev, path]
    );
  }

  async function run() {
    if (!query.trim() || busy) return;
    setBusy(true);
    setSummary("");
    setPerSource("");
    setCitations([]);
    setResults([]);

    const scope = resolveScope();

    try {
      if (mode === "ask") {
        setStreaming(true);
        await ask(
          query,
          {
            onCitations: (c) => setCitations(c),
            onSection: () => {},
            onToken: (section, t) => {
              if (section === "summary") setSummary((prev) => prev + t);
              else setPerSource((prev) => prev + t);
            },
          },
          scope
        );
        setStreaming(false);
      } else {
        const res = await search(query, scope);
        setResults(res.results);
      }
    } catch (e: any) {
      setNotice(`Error: ${e.message ?? e}`);
    } finally {
      setBusy(false);
      setStreaming(false);
    }
  }

  function onIndexProgress(s: IndexStatus) {
    if (s.status === "running") {
      const filePart = s.current_file ? ` — ${s.current_file}` : "";
      const chunkPart =
        s.file_chunk_total && s.file_chunk_total > 0
          ? ` (${s.file_chunk_done}/${s.file_chunk_total} chunks)`
          : " (reading…)";
      const skipPart = s.skipped_files ? `, ${s.skipped_files} skipped` : "";
      setNotice(
        `Indexing ${s.processed_files}/${s.total_files}${filePart}${chunkPart}${skipPart}`
      );
    } else if (s.message) {
      setNotice(s.message);
    }
  }

  async function runIndex(force = false) {
    if (indexing) return;
    setIndexing(true);
    setNotice(force ? "Re-indexing everything…" : "Indexing new/changed files…");
    try {
      const res = await indexAll(onIndexProgress, force);
      setNotice(res.message || `Done. ${res.total_chunks} chunks in archive.`);
      refreshStatus();
    } catch (e: any) {
      setNotice(`Index failed: ${e.message ?? e}`);
    } finally {
      setIndexing(false);
    }
  }

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setNotice("Uploading…");
    try {
      const up = await uploadFiles(files);
      setNotice(`Uploaded ${up.saved.length} file(s). Starting indexing…`);
      await runIndex(false);
    } catch (e: any) {
      setNotice(`Upload/index failed: ${e.message ?? e}`);
    }
  }

  async function onImportFolder() {
    if (!folderPath.trim() || indexing) return;
    setNotice(`Importing from ${folderPath}…`);
    try {
      const imp = await importFolder(folderPath.trim());
      setNotice(
        `Imported ${imp.imported} of ${imp.found} file(s) into ${imp.into}. Indexing…`
      );
      await runIndex(false);
    } catch (e: any) {
      setNotice(`Folder import failed: ${e.message ?? e}`);
    }
  }

  // ------------------------------------------------------------------
  // Group mutations — each refreshes the groups list afterwards.
  // ------------------------------------------------------------------

  async function onCreateGroup() {
    const name = newGroupName.trim();
    if (!name) return;
    try {
      await createGroup(name);
      setNewGroupName("");
      await refreshScope();
    } catch (e: any) {
      setNotice(`Create group failed: ${e.message ?? e}`);
    }
  }

  async function onRenameGroup(id: string, currentName: string) {
    const name = window.prompt("Rename group", currentName);
    if (name === null) return;
    if (!name.trim()) return;
    try {
      await renameGroup(id, name.trim());
      await refreshScope();
    } catch (e: any) {
      setNotice(`Rename failed: ${e.message ?? e}`);
    }
  }

  async function onDeleteGroup(id: string) {
    if (!window.confirm("Delete this group? Sources stay in the archive.")) return;
    try {
      await deleteGroup(id);
      if (editGroupId === id) setEditGroupId("");
      await refreshScope();
    } catch (e: any) {
      setNotice(`Delete failed: ${e.message ?? e}`);
    }
  }

  async function onToggleMember(groupId: string, sourcePath: string, isMember: boolean) {
    try {
      if (isMember) await removeSources(groupId, [sourcePath]);
      else await addSources(groupId, [sourcePath]);
      await refreshScope();
    } catch (e: any) {
      setNotice(`Update group failed: ${e.message ?? e}`);
    }
  }

  const editingGroup = useMemo(
    () => groups.find((g) => g.group_id === editGroupId) ?? null,
    [groups, editGroupId]
  );

  const modelsReady = health?.models.llm_ready && health?.models.embed_ready;

  return (
    <div className="container">
      <div className="header">
        <div>
          <div className="title">
            Smart <span>Archive</span>
          </div>
          <div className="subtitle">
            Hybrid semantic + keyword search over your local archive, curated by
            a local LLM
          </div>
        </div>
        <div className="stats">
          {stats && (
            <span className="stat">
              <b>{stats.total_chunks}</b> chunks ·{" "}
              <b>{Object.keys(stats.sources).length}</b> sources
            </span>
          )}
        </div>
      </div>

      {!modelsReady && health && (
        <div className="panel" style={{ borderColor: "var(--danger)" }}>
          <div className="muted">
            Models not fully ready. LLM ready:{" "}
            {String(health.models.llm_ready)} · Embeddings ready:{" "}
            {String(health.models.embed_ready)}. Make sure Ollama is running and
            both models are pulled.
          </div>
        </div>
      )}

      <div className="modes">
        <div
          className={`chip ${mode === "ask" ? "active" : ""}`}
          onClick={() => setMode("ask")}
        >
          Ask (AI answer)
        </div>
        <div
          className={`chip ${mode === "search" ? "active" : ""}`}
          onClick={() => setMode("search")}
        >
          Search (raw chunks)
        </div>
      </div>

      <div className="searchbar">
        <input
          className="input"
          placeholder={
            mode === "ask"
              ? "Ask anything about your archive…"
              : "Search for terms, names, topics…"
          }
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
        />
        <button className="btn" onClick={run} disabled={busy}>
          {busy ? <span className="spinner" /> : mode === "ask" ? "Ask" : "Search"}
        </button>
      </div>

      {/* Scope control */}
      <div className="scope-panel">
        <div className="scope-modes">
          <div
            className={`chip ${scopeMode === "archive" ? "active" : ""}`}
            onClick={() => setScopeMode("archive")}
          >
            Whole archive
          </div>
          <div
            className={`chip ${scopeMode === "sources" ? "active" : ""}`}
            onClick={() => setScopeMode("sources")}
          >
            Selected sources
          </div>
          <div
            className={`chip ${scopeMode === "group" ? "active" : ""}`}
            onClick={() => setScopeMode("group")}
          >
            Group
          </div>
        </div>

        {scopeMode === "sources" && (
          <>
            <div className="row" style={{ marginBottom: 8 }}>
              <span className="muted">
                {selectedSources.length} of {selectable.length} selected
              </span>
              {selectedSources.length > 0 && (
                <button
                  className="link-btn"
                  onClick={() => setSelectedSources([])}
                >
                  Clear
                </button>
              )}
            </div>
            {selectable.length === 0 ? (
              <div className="muted">No indexed sources yet.</div>
            ) : (
              <div className="source-list">
                {selectable.map((s) => (
                  <label className="source-row" key={s.source_path}>
                    <input
                      type="checkbox"
                      checked={selectedSources.includes(s.source_path)}
                      onChange={() => toggleSource(s.source_path)}
                    />
                    <span className="source-name">{s.source_path}</span>
                    <span className="source-count">{s.chunks} chunks</span>
                  </label>
                ))}
              </div>
            )}
          </>
        )}

        {scopeMode === "group" && (
          <div className="row">
            {groups.length === 0 ? (
              <span className="muted">
                No groups yet — create one in the manager below.
              </span>
            ) : (
              <select
                className="select"
                value={selectedGroupId}
                onChange={(e) => setSelectedGroupId(e.target.value)}
              >
                <option value="">Choose a group…</option>
                {groups.map((g) => (
                  <option key={g.group_id} value={g.group_id}>
                    {g.name} ({g.members.length})
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        <div className="scope-active">
          Scope: <b>{activeScopeLabel}</b>
        </div>
      </div>

      {notice && <div className="muted" style={{ marginBottom: 16 }}>{notice}</div>}

      {/* Synthesized summary */}
      {mode === "ask" && (summary || streaming) && (
        <div className="panel">
          <div className="section-label">Answer</div>
          <div className="answer">
            {summary}
            {streaming && !perSource && <span className="cursor" />}
          </div>
        </div>
      )}

      {/* Per-source findings */}
      {mode === "ask" && (perSource || (streaming && summary)) && (
        <div className="panel">
          <div className="section-label">Per-source findings</div>
          <div className="answer">
            {perSource}
            {streaming && perSource && <span className="cursor" />}
          </div>
        </div>
      )}

      {/* Citations */}
      {mode === "ask" && citations.length > 0 && (
        <div className="panel">
          <div className="section-label">Sources</div>
          {citations.map((c) => (
            <div className="citation" key={c.index}>
              <span className="cite-index">[{c.index}]</span>
              <span>
                <b>{c.source_file}</b>
                {c.location ? ` — ${c.location}` : ""}{" "}
                <span className="muted">
                  {c.matched_by.join(" + ")}
                </span>
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Raw search results */}
      {mode === "search" && results.length > 0 && (
        <div className="panel">
          <div className="section-label">{results.length} results</div>
          {results.map((r) => (
            <div className="result" key={r.id}>
              <div className="result-meta">
                <b style={{ color: "var(--text)" }}>
                  {r.metadata.source_file}
                </b>
                <span>{r.metadata.location}</span>
                <span className="tag">{r.metadata.file_type}</span>
                {r.matched_by.map((m) => (
                  <span key={m} className={`tag ${m}`}>
                    {m}
                  </span>
                ))}
              </div>
              <div className="result-text">
                {r.text.length > 400 ? r.text.slice(0, 400) + "…" : r.text}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Groups manager */}
      <div className="panel">
        <div
          className="collapse-toggle section-label"
          onClick={() => setManagerOpen((o) => !o)}
        >
          <span>Groups ({groups.length})</span>
          <span className="caret">{managerOpen ? "▲ hide" : "▼ manage"}</span>
        </div>

        {managerOpen && (
          <>
            <div className="row" style={{ marginBottom: 16 }}>
              <input
                className="input"
                placeholder="New group name…"
                value={newGroupName}
                onChange={(e) => setNewGroupName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && onCreateGroup()}
              />
              <button
                className="btn secondary"
                onClick={onCreateGroup}
                disabled={!newGroupName.trim()}
              >
                Create
              </button>
            </div>

            {groups.length === 0 ? (
              <div className="muted">No groups yet.</div>
            ) : (
              groups.map((g) => (
                <div className="group-row" key={g.group_id}>
                  <span className="group-name">{g.name}</span>
                  <span className="group-count">{g.members.length} sources</span>
                  <div className="group-actions">
                    <button
                      className="link-btn"
                      onClick={() =>
                        setEditGroupId((id) =>
                          id === g.group_id ? "" : g.group_id
                        )
                      }
                    >
                      {editGroupId === g.group_id ? "Done" : "Edit sources"}
                    </button>
                    <button
                      className="link-btn"
                      onClick={() => onRenameGroup(g.group_id, g.name)}
                    >
                      Rename
                    </button>
                    <button
                      className="link-btn"
                      style={{ color: "var(--danger)" }}
                      onClick={() => onDeleteGroup(g.group_id)}
                    >
                      Delete
                    </button>
                  </div>
                </div>
              ))
            )}

            {editingGroup && (
              <div style={{ marginTop: 16 }}>
                <div className="section-label">
                  Members of “{editingGroup.name}”
                </div>
                {selectable.length === 0 &&
                editingGroup.members.length === 0 ? (
                  <div className="muted">No indexed sources yet.</div>
                ) : (
                  <div className="source-list">
                    {/* Currently-selectable (indexed) sources */}
                    {selectable.map((s) => {
                      const isMember = editingGroup.members.includes(
                        s.source_path
                      );
                      return (
                        <label className="source-row" key={s.source_path}>
                          <input
                            type="checkbox"
                            checked={isMember}
                            onChange={() =>
                              onToggleMember(
                                editingGroup.group_id,
                                s.source_path,
                                isMember
                              )
                            }
                          />
                          <span className="source-name">{s.source_path}</span>
                          <span className="source-count">
                            {s.chunks} chunks
                          </span>
                        </label>
                      );
                    })}
                    {/* Stale members: in the group but not currently indexed */}
                    {editingGroup.members
                      .filter(
                        (m) =>
                          !selectable.some((s) => s.source_path === m)
                      )
                      .map((m) => (
                        <label className="source-row dim" key={m}>
                          <input
                            type="checkbox"
                            checked
                            onChange={() =>
                              onToggleMember(editingGroup.group_id, m, true)
                            }
                          />
                          <span className="source-name">{m}</span>
                          <span className="source-count">not indexed</span>
                        </label>
                      ))}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      {/* Upload / index */}
      <div className="panel">
        <div className="section-label">Add to archive</div>
        <div
          className="dropzone"
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            onUpload(e.dataTransfer.files);
          }}
        >
          Drop files here or click to upload
          <div className="muted" style={{ marginTop: 6 }}>
            {stats
              ? `Supported: ${stats.supported_extensions.join(", ")}`
              : "PDF, docx, csv, epub, txt, md, html"}
          </div>
        </div>
        <input
          ref={fileInput}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={(e) => onUpload(e.target.files)}
        />

        {/* Import an existing folder from disk */}
        <div className="searchbar" style={{ marginTop: 16 }}>
          <input
            className="input"
            placeholder="Import a folder path (e.g. /Users/you/Documents/books)"
            value={folderPath}
            onChange={(e) => setFolderPath(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && onImportFolder()}
          />
          <button
            className="btn secondary"
            onClick={onImportFolder}
            disabled={indexing || !folderPath.trim()}
          >
            Import
          </button>
        </div>

        {/* Re-index controls */}
        <div className="modes" style={{ marginTop: 16, marginBottom: 0 }}>
          <button
            className="btn secondary"
            onClick={() => runIndex(false)}
            disabled={indexing}
          >
            {indexing ? <span className="spinner" /> : "Index new / changed"}
          </button>
          <button
            className="btn secondary"
            onClick={() => runIndex(true)}
            disabled={indexing}
            title="Re-embed every file, ignoring the manifest"
          >
            Force re-index all
          </button>
          {stats && (
            <span className="muted" style={{ alignSelf: "center", marginLeft: "auto" }}>
              {stats.indexed_files} file(s) indexed
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
