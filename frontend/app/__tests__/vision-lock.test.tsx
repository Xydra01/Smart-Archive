import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Mock the API client so no network is hit. We keep the *real*
// IndexingInProgressError class (via importActual) so `e instanceof
// IndexingInProgressError` in page.tsx's run() still matches what the tests
// throw. Every other export is a vi.fn we control per test.
vi.mock("../api", async (importActual) => {
  const actual = await importActual<typeof import("../api")>();
  return {
    IndexingInProgressError: actual.IndexingInProgressError,
    getStats: vi.fn(),
    getHealth: vi.fn(),
    getSelectableSources: vi.fn(),
    listGroups: vi.fn(),
    getIndexStatus: vi.fn(),
    search: vi.fn(),
    ask: vi.fn(),
    createGroup: vi.fn(),
    renameGroup: vi.fn(),
    deleteGroup: vi.fn(),
    addSources: vi.fn(),
    removeSources: vi.fn(),
    uploadFiles: vi.fn(),
    importFolder: vi.fn(),
    indexAll: vi.fn(),
  };
});

import Home from "../page";
import * as api from "../api";
import { IndexingInProgressError } from "../api";

// Mount-time loader defaults so the page renders without throwing. Individual
// tests override getIndexStatus / search as needed.
function mockDefaults() {
  vi.mocked(api.getStats).mockResolvedValue({
    total_chunks: 22,
    sources: { "/archive/alpha.pdf": 12 },
    supported_extensions: [".pdf", ".txt"],
    indexed_files: 1,
    manifest: {},
  } as any);
  vi.mocked(api.getHealth).mockResolvedValue({
    status: "ok",
    models: {
      ollama_reachable: true,
      llm_model: "llm",
      llm_ready: true,
      embed_model: "embed",
      embed_ready: true,
      installed_models: [],
    },
  } as any);
  vi.mocked(api.getSelectableSources).mockResolvedValue({ sources: [] } as any);
  vi.mocked(api.listGroups).mockResolvedValue({ groups: [] } as any);
  vi.mocked(api.search).mockResolvedValue({ results: [] } as any);
  // Default: not indexing. Tests that exercise the lock override this.
  vi.mocked(api.getIndexStatus).mockResolvedValue({ status: "done" } as any);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockDefaults();
});

afterEach(() => {
  // Ensure no fake timers leak between tests.
  vi.useRealTimers();
});

// The run button is labelled "Ask" in ask mode and "Search" in search mode.
// While indexing it stays labelled but disabled.
function askButton() {
  return screen.getByRole("button", { name: "Ask" });
}

async function switchToSearch(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText("Search (raw chunks)"));
}

describe("vision-ingest querying lock", () => {
  it("disables querying while indexing", async () => {
    vi.mocked(api.getIndexStatus).mockResolvedValue({
      status: "running",
      current_file: "book.pdf",
    } as any);

    render(<Home />);

    // The initial poll tick fires on mount; wait for the lock to apply.
    await waitFor(() => expect(askButton()).toBeDisabled());

    // The paused panel is shown.
    expect(
      screen.getByText(/indexing in progress|querying is paused/i)
    ).toBeInTheDocument();

    // The query input is disabled too.
    const input = screen.getByPlaceholderText(/querying paused while indexing/i);
    expect(input).toBeDisabled();
  });

  it("re-enables querying when indexing finishes", async () => {
    // Fake timers + React 19 act() are fiddly to combine, so per the task's
    // fallback we verify the finished state directly: with status "done" from
    // the start the controls are enabled and no paused panel is shown.
    vi.mocked(api.getIndexStatus).mockResolvedValue({ status: "done" } as any);

    render(<Home />);

    await waitFor(() => expect(askButton()).toBeEnabled());

    // The query input is usable (not disabled).
    const input = screen.getByPlaceholderText(/Ask anything about your archive/i);
    expect(input).toBeEnabled();

    // The paused panel is absent.
    expect(
      screen.queryByText(
        /indexing in progress — querying is paused and will resume/i
      )
    ).not.toBeInTheDocument();
  });

  it("shows paused state when a query is refused with IndexingInProgressError", async () => {
    // Not initially locked.
    vi.mocked(api.getIndexStatus).mockResolvedValue({ status: "done" } as any);
    // The query itself is refused because a job started server-side.
    vi.mocked(api.search).mockRejectedValue(new IndexingInProgressError());

    const user = userEvent.setup();
    render(<Home />);

    await waitFor(() => expect(askButton()).toBeEnabled());
    await switchToSearch(user);

    const input = screen.getByPlaceholderText(/Search for terms/i);
    await user.type(input, "photons");
    await user.click(screen.getByRole("button", { name: "Search" }));

    // run()'s catch handler flips indexing on, which renders the paused panel
    // and sets the paused notice — both match, so assert at least one appears.
    await waitFor(() =>
      expect(
        screen.getAllByText(/indexing in progress|querying is paused/i).length
      ).toBeGreaterThan(0)
    );
  });

  it("renders content_type tags on search results", async () => {
    vi.mocked(api.getIndexStatus).mockResolvedValue({ status: "done" } as any);
    vi.mocked(api.search).mockResolvedValue({
      results: [
        {
          id: "c1",
          text: "A chart of quarterly revenue.",
          metadata: {
            source_file: "report.pdf",
            location: "p. 4",
            file_type: "pdf",
            content_type: "chart",
          },
          matched_by: ["semantic"],
          rrf_score: 1,
          semantic_score: 0.9,
          keyword_score: null,
        },
      ],
    } as any);

    const user = userEvent.setup();
    render(<Home />);

    await waitFor(() => expect(askButton()).toBeEnabled());
    await switchToSearch(user);

    const input = screen.getByPlaceholderText(/Search for terms/i);
    await user.type(input, "revenue");
    await user.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() =>
      expect(screen.getByText("chart")).toBeInTheDocument()
    );
  });
});
