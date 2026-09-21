import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Mock the API client module so no network is hit. Each exported function is a
// vi.fn we can assert against; mount-time loaders return controlled data.
vi.mock("../api", () => {
  return {
    getStats: vi.fn(),
    getHealth: vi.fn(),
    getSelectableSources: vi.fn(),
    listGroups: vi.fn(),
    search: vi.fn(),
    ask: vi.fn(),
    createGroup: vi.fn(),
    renameGroup: vi.fn(),
    deleteGroup: vi.fn(),
    addSources: vi.fn(),
    removeSources: vi.fn(),
    // Unused-by-these-tests functions still referenced by the page import.
    uploadFiles: vi.fn(),
    importFolder: vi.fn(),
    indexAll: vi.fn(),
  };
});

import Home from "../page";
import * as api from "../api";

const SOURCES = [
  { source_path: "/archive/alpha.pdf", chunks: 12 },
  { source_path: "/archive/beta.pdf", chunks: 7 },
  { source_path: "/archive/gamma.txt", chunks: 3 },
];

const GROUPS = [
  { group_id: "grp-1", name: "Study", members: ["/archive/alpha.pdf"] },
  { group_id: "grp-2", name: "Reference", members: [] },
];

function mockDefaults() {
  vi.mocked(api.getStats).mockResolvedValue({
    total_chunks: 22,
    sources: {
      "/archive/alpha.pdf": 12,
      "/archive/beta.pdf": 7,
      "/archive/gamma.txt": 3,
    },
    supported_extensions: [".pdf", ".txt"],
    indexed_files: 3,
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
  vi.mocked(api.getSelectableSources).mockResolvedValue({ sources: SOURCES } as any);
  vi.mocked(api.listGroups).mockResolvedValue({ groups: GROUPS } as any);
  vi.mocked(api.search).mockResolvedValue({ results: [] } as any);
  vi.mocked(api.createGroup).mockResolvedValue(GROUPS[0] as any);
  vi.mocked(api.renameGroup).mockResolvedValue(GROUPS[0] as any);
  vi.mocked(api.deleteGroup).mockResolvedValue(undefined as any);
  vi.mocked(api.addSources).mockResolvedValue(GROUPS[0] as any);
  vi.mocked(api.removeSources).mockResolvedValue(GROUPS[0] as any);
}

// Renders the page and waits for the mount-time loaders to resolve so scope
// data (sources + groups) is present before the test interacts with the UI.
async function renderPage() {
  render(<Home />);
  await waitFor(() => expect(api.getSelectableSources).toHaveBeenCalled());
  await waitFor(() => expect(api.listGroups).toHaveBeenCalled());
}

// Switches the run mode to "Search" so run() calls the (easily-asserted)
// search() client instead of the streaming ask().
async function useSearchMode(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText("Search (raw chunks)"));
}

async function typeQuery(user: ReturnType<typeof userEvent.setup>, text: string) {
  const input = screen.getByPlaceholderText(/Search for terms/i);
  await user.clear(input);
  await user.type(input, text);
}

// The run button is labelled "Search" in search mode. It is the last such
// control; scope chip "Selected sources" also matches /search/i by role, so
// query by exact button name.
function runButton() {
  return screen.getByRole("button", { name: "Search" });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockDefaults();
});

describe("scope controls (Req 15.1)", () => {
  it("renders selectable sources as options in 'Selected sources' mode", async () => {
    const user = userEvent.setup();
    await renderPage();

    // In the default "Whole archive" mode the source list is not shown.
    expect(screen.queryByText("/archive/alpha.pdf")).not.toBeInTheDocument();

    await user.click(screen.getByText("Selected sources"));

    // Each API-provided source is rendered with a checkbox and its path.
    for (const s of SOURCES) {
      expect(screen.getByText(s.source_path)).toBeInTheDocument();
    }
    // One checkbox per selectable source.
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(SOURCES.length);
    expect(screen.getByText(`0 of ${SOURCES.length} selected`)).toBeInTheDocument();
  });
});

describe("search scoping", () => {
  it("sends { sources: [...] } when sources are selected (Req 15.2)", async () => {
    const user = userEvent.setup();
    await renderPage();
    await useSearchMode(user);

    await user.click(screen.getByText("Selected sources"));

    // Select the first two sources via their row labels.
    const alpha = screen.getByText("/archive/alpha.pdf").closest("label")!;
    const beta = screen.getByText("/archive/beta.pdf").closest("label")!;
    await user.click(within(alpha).getByRole("checkbox"));
    await user.click(within(beta).getByRole("checkbox"));

    await typeQuery(user, "photons");
    await user.click(runButton());

    await waitFor(() => expect(api.search).toHaveBeenCalledTimes(1));
    expect(api.search).toHaveBeenCalledWith("photons", {
      sources: ["/archive/alpha.pdf", "/archive/beta.pdf"],
    });
  });

  it("sends { group_id } when a group is chosen (Req 15.3)", async () => {
    const user = userEvent.setup();
    await renderPage();
    await useSearchMode(user);

    await user.click(screen.getByText("Group"));

    const select = screen.getByRole("combobox");
    await user.selectOptions(select, "grp-1");

    await typeQuery(user, "derivatives");
    await user.click(runButton());

    await waitFor(() => expect(api.search).toHaveBeenCalledTimes(1));
    expect(api.search).toHaveBeenCalledWith("derivatives", { group_id: "grp-1" });
  });

  it("sends no scope in 'Whole archive' mode (Req 15.4)", async () => {
    const user = userEvent.setup();
    await renderPage();
    await useSearchMode(user);

    await typeQuery(user, "anything");
    await user.click(runButton());

    await waitFor(() => expect(api.search).toHaveBeenCalledTimes(1));
    expect(api.search).toHaveBeenCalledWith("anything", undefined);
  });

  it("omits scope when 'Selected sources' mode has nothing picked (Req 15.4)", async () => {
    const user = userEvent.setup();
    await renderPage();
    await useSearchMode(user);

    await user.click(screen.getByText("Selected sources"));
    await typeQuery(user, "nothing picked");
    await user.click(runButton());

    await waitFor(() => expect(api.search).toHaveBeenCalledTimes(1));
    expect(api.search).toHaveBeenCalledWith("nothing picked", undefined);
  });
});

describe("groups manager (Req 15.5)", () => {
  async function openManager(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByText(/manage/i));
  }

  it("create invokes createGroup with the typed name", async () => {
    const user = userEvent.setup();
    await renderPage();
    await openManager(user);

    const nameInput = screen.getByPlaceholderText("New group name…");
    await user.type(nameInput, "New Topic");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(api.createGroup).toHaveBeenCalledTimes(1));
    expect(api.createGroup).toHaveBeenCalledWith("New Topic");
  });

  it("delete invokes deleteGroup with the group id", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    await renderPage();
    await openManager(user);

    // First group row's Delete button.
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    await user.click(deleteButtons[0]);

    await waitFor(() => expect(api.deleteGroup).toHaveBeenCalledTimes(1));
    expect(api.deleteGroup).toHaveBeenCalledWith("grp-1");
    confirmSpy.mockRestore();
  });

  it("rename invokes renameGroup with the prompted name", async () => {
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("Renamed");
    const user = userEvent.setup();
    await renderPage();
    await openManager(user);

    const renameButtons = screen.getAllByRole("button", { name: "Rename" });
    await user.click(renameButtons[0]);

    await waitFor(() => expect(api.renameGroup).toHaveBeenCalledTimes(1));
    expect(api.renameGroup).toHaveBeenCalledWith("grp-1", "Renamed");
    promptSpy.mockRestore();
  });

  it("editing sources add/removes members via addSources/removeSources", async () => {
    const user = userEvent.setup();
    await renderPage();
    await openManager(user);

    // Open the source editor for the first group ("Study", member: alpha).
    const editButtons = screen.getAllByRole("button", { name: "Edit sources" });
    await user.click(editButtons[0]);

    // The members editor lists the selectable sources with checkboxes.
    await waitFor(() =>
      expect(screen.getByText(/Members of/i)).toBeInTheDocument()
    );

    const editor = screen.getByText(/Members of/i).closest("div")!.parentElement!;
    const betaRow = within(editor).getByText("/archive/beta.pdf").closest("label")!;
    const alphaRow = within(editor).getByText("/archive/alpha.pdf").closest("label")!;

    // beta is not a member -> checking it adds it.
    await user.click(within(betaRow).getByRole("checkbox"));
    await waitFor(() => expect(api.addSources).toHaveBeenCalledTimes(1));
    expect(api.addSources).toHaveBeenCalledWith("grp-1", ["/archive/beta.pdf"]);

    // alpha is a member -> unchecking it removes it.
    await user.click(within(alphaRow).getByRole("checkbox"));
    await waitFor(() => expect(api.removeSources).toHaveBeenCalledTimes(1));
    expect(api.removeSources).toHaveBeenCalledWith("grp-1", ["/archive/alpha.pdf"]);
  });
});
