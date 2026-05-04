import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef, useState, useEffect } from "react";
import {
  fetchKnowledgeTree,
  fetchKnowledgeArticle,
  createKnowledgeFolder,
  createKnowledgeArticle,
  uploadKnowledgeArticle,
  updateKnowledgeNode,
  deleteKnowledgeNode,
  type KnowledgeNode,
} from "@/lib/api-client";
import { renderMarkdown } from "@/lib/markdown";
import { exportArticleAsPdf } from "@/lib/pdf-export";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  FilePlus,
  FolderPlus,
  Upload,
  Trash2,
  Save,
  Pencil,
  X,
  FileText,
  FileType,
  File as FileIcon,
  Search,
  Download,
  FileDown,
} from "lucide-react";
import { toast } from "sonner";

// Search-param schema. Other surfaces (Value & Readiness proposal generator,
// future cross-app links) deeplink into a specific article via
// `/knowledge?node=<id>`. We validate it here so TanStack Router types it
// correctly and the page can react via `useSearch()`.
type KnowledgeSearch = { node?: string };

export const Route = createFileRoute("/_sidebar/knowledge")({
  component: KnowledgePage,
  validateSearch: (search: Record<string, unknown>): KnowledgeSearch => ({
    node:
      typeof search.node === "string" && search.node ? search.node : undefined,
  }),
});

// ---------------------------------------------------------------------------
// Tree helpers
// ---------------------------------------------------------------------------

interface TreeNode extends KnowledgeNode {
  children: TreeNode[];
}

function buildTree(flat: KnowledgeNode[]): TreeNode[] {
  const byId = new Map<string, TreeNode>();
  flat.forEach((n) => byId.set(n.node_id, { ...n, children: [] }));
  const roots: TreeNode[] = [];
  byId.forEach((node) => {
    const parent = node.parent_id ? byId.get(node.parent_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  });
  // Folders first, then alpha within each level. Server already sorts mostly
  // this way, but rebuilding the tree client-side can shuffle siblings if
  // some IDs missed parents (orphans become roots).
  const sortNodes = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      if (a.node_type !== b.node_type) {
        return a.node_type === "folder" ? -1 : 1;
      }
      return a.title.localeCompare(b.title);
    });
    nodes.forEach((n) => sortNodes(n.children));
  };
  sortNodes(roots);
  return roots;
}

function fileIcon(node: KnowledgeNode) {
  if (node.node_type === "folder") return null;
  switch (node.content_format) {
    case "markdown":
      return <FileText size={14} className="text-blue-500" />;
    case "pdf":
      return <FileType size={14} className="text-red-500" />;
    case "docx":
      return <FileType size={14} className="text-indigo-500" />;
    default:
      return <FileIcon size={14} />;
  }
}

function formatBytes(n: number): string {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// ---------------------------------------------------------------------------
// Tree view
// ---------------------------------------------------------------------------

interface TreeViewProps {
  nodes: TreeNode[];
  selectedId: string | null;
  expanded: Set<string>;
  onToggle: (id: string) => void;
  onSelect: (node: KnowledgeNode) => void;
  depth?: number;
}

function TreeView({
  nodes,
  selectedId,
  expanded,
  onToggle,
  onSelect,
  depth = 0,
}: TreeViewProps) {
  return (
    <ul className="space-y-0.5">
      {nodes.map((n) => {
        const isOpen = expanded.has(n.node_id);
        const isSelected = selectedId === n.node_id;
        return (
          <li key={n.node_id}>
            <div
              className={`flex items-center gap-1 rounded px-1 py-1 text-sm cursor-pointer hover:bg-accent ${
                isSelected ? "bg-accent text-accent-foreground" : ""
              }`}
              style={{ paddingLeft: `${depth * 12 + 4}px` }}
              onClick={() => {
                if (n.node_type === "folder") onToggle(n.node_id);
                onSelect(n);
              }}
            >
              {n.node_type === "folder" ? (
                <>
                  {isOpen ? (
                    <ChevronDown size={14} className="shrink-0" />
                  ) : (
                    <ChevronRight size={14} className="shrink-0" />
                  )}
                  {isOpen ? (
                    <FolderOpen size={14} className="shrink-0 text-amber-500" />
                  ) : (
                    <Folder size={14} className="shrink-0 text-amber-500" />
                  )}
                </>
              ) : (
                <>
                  <span className="w-[14px] shrink-0" />
                  {fileIcon(n)}
                </>
              )}
              <span className="truncate" title={n.title}>
                {n.title}
              </span>
            </div>
            {n.node_type === "folder" && isOpen && n.children.length > 0 && (
              <TreeView
                nodes={n.children}
                selectedId={selectedId}
                expanded={expanded}
                onToggle={onToggle}
                onSelect={onSelect}
                depth={depth + 1}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Preview panes
// ---------------------------------------------------------------------------

function FolderPreview({
  node,
  childCount,
  onCreateFolder,
  onCreateArticle,
  onUpload,
  onDelete,
  onRename,
}: {
  node: KnowledgeNode | null; // null = root
  childCount: number;
  onCreateFolder: () => void;
  onCreateArticle: () => void;
  onUpload: () => void;
  onDelete: () => void;
  onRename: () => void;
}) {
  const isRoot = !node;
  return (
    <Card className="h-full">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2">
              <Folder className="h-5 w-5 text-amber-500" />
              <span className="truncate">{isRoot ? "Knowledge Base" : node!.title}</span>
            </CardTitle>
            {node?.summary && (
              <p className="text-sm text-muted-foreground mt-1">{node.summary}</p>
            )}
            <p className="text-xs text-muted-foreground mt-2">
              {childCount} item{childCount === 1 ? "" : "s"}
            </p>
          </div>
          {!isRoot && (
            <div className="flex gap-1">
              <Button size="sm" variant="ghost" onClick={onRename} title="Rename">
                <Pencil className="h-4 w-4" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={onDelete}
                title="Delete folder"
              >
                <Trash2 className="h-4 w-4 text-destructive" />
              </Button>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          {isRoot
            ? "Use the buttons below to add folders or articles at the top level. Articles can be markdown you write in-app, PDFs, DOCX, or .md files you upload."
            : "Add items inside this folder, or use the toolbar to upload files."}
        </p>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={onCreateFolder}>
            <FolderPlus className="h-4 w-4 mr-1" /> New Folder
          </Button>
          <Button size="sm" variant="outline" onClick={onCreateArticle}>
            <FilePlus className="h-4 w-4 mr-1" /> New Article
          </Button>
          <Button size="sm" variant="outline" onClick={onUpload}>
            <Upload className="h-4 w-4 mr-1" /> Upload File
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function ArticlePreview({
  nodeId,
  onDelete,
  onAfterEdit,
}: {
  nodeId: string;
  onDelete: () => void;
  onAfterEdit: () => void;
}) {
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["knowledgeArticle", nodeId],
    queryFn: () => fetchKnowledgeArticle(nodeId),
  });

  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editSummary, setEditSummary] = useState("");
  const [editTags, setEditTags] = useState("");
  const [editBody, setEditBody] = useState("");

  useEffect(() => {
    setEditing(false);
  }, [nodeId]);

  useEffect(() => {
    if (data?.node) {
      setEditTitle(data.node.title);
      setEditSummary(data.node.summary);
      setEditTags(data.node.tags.join(", "));
      setEditBody(data.body_markdown);
    }
  }, [data]);

  const saveMutation = useMutation({
    mutationFn: () => {
      const body: Parameters<typeof updateKnowledgeNode>[1] = {
        title: editTitle,
        summary: editSummary,
        tags: editTags,
      };
      if (data?.node.content_format === "markdown") {
        body.content_md = editBody;
      }
      return updateKnowledgeNode(nodeId, body);
    },
    onSuccess: () => {
      toast.success("Saved");
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: ["knowledgeTree"] });
      queryClient.invalidateQueries({ queryKey: ["knowledgeArticle", nodeId] });
      onAfterEdit();
    },
    onError: (e: Error) => toast.error(e.message),
  });

  if (isLoading) {
    return (
      <Card className="h-full">
        <CardContent className="p-6 text-sm text-muted-foreground">
          Loading article…
        </CardContent>
      </Card>
    );
  }
  if (error || !data) {
    return (
      <Card className="h-full">
        <CardContent className="p-6 text-sm text-destructive">
          Could not load article.
        </CardContent>
      </Card>
    );
  }

  const node = data.node;
  const isMd = node.content_format === "markdown";
  const isPdf = node.content_format === "pdf";

  return (
    <Card className="h-full flex flex-col">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            {editing ? (
              <Input
                value={editTitle}
                onChange={(e) => setEditTitle(e.target.value)}
                className="text-lg font-semibold"
              />
            ) : (
              <CardTitle className="flex items-center gap-2">
                {fileIcon(node)}
                <span className="truncate">{node.title}</span>
              </CardTitle>
            )}
            <div className="flex flex-wrap gap-2 items-center mt-2 text-xs text-muted-foreground">
              <Badge variant="outline">{node.content_format || "folder"}</Badge>
              {node.file_size_bytes > 0 && <span>{formatBytes(node.file_size_bytes)}</span>}
              {node.updated_at && (
                <span>
                  Updated {new Date(node.updated_at).toLocaleString()}
                </span>
              )}
              {node.created_by && <span>by {node.created_by}</span>}
            </div>
            {editing ? (
              <Input
                value={editSummary}
                onChange={(e) => setEditSummary(e.target.value)}
                placeholder="Short summary"
                className="mt-2"
              />
            ) : (
              node.summary && (
                <p className="text-sm text-muted-foreground mt-2">{node.summary}</p>
              )
            )}
            {editing ? (
              <Input
                value={editTags}
                onChange={(e) => setEditTags(e.target.value)}
                placeholder="comma, separated, tags"
                className="mt-2"
              />
            ) : (
              node.tags.length > 0 && (
                <div className="flex flex-wrap gap-1 mt-2">
                  {node.tags.map((t) => (
                    <Badge key={t} variant="secondary" className="text-xs">
                      {t}
                    </Badge>
                  ))}
                </div>
              )
            )}
          </div>
          <div className="flex flex-col gap-1 shrink-0">
            {editing ? (
              <>
                <Button
                  size="sm"
                  onClick={() => saveMutation.mutate()}
                  disabled={saveMutation.isPending}
                >
                  <Save className="h-4 w-4 mr-1" /> Save
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                  <X className="h-4 w-4 mr-1" /> Cancel
                </Button>
              </>
            ) : (
              <>
                <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
                  <Pencil className="h-4 w-4 mr-1" /> Edit
                </Button>
                {isMd && (
                  // Markdown articles aren't files — render them through
                  // the browser's print pipeline so users get a real
                  // selectable-text PDF (no rasterized canvas snapshots).
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      try {
                        const updatedAt = node.updated_at
                          ? new Date(node.updated_at).toLocaleDateString()
                          : "";
                        const metaParts = [
                          updatedAt ? `Updated ${updatedAt}` : "",
                          node.created_by ? `by ${node.created_by}` : "",
                          node.version > 1 ? `v${node.version}` : "",
                        ].filter(Boolean);
                        exportArticleAsPdf({
                          title: node.title,
                          bodyMarkdown: data.body_markdown,
                          meta: metaParts.join(" · "),
                        });
                      } catch (e: any) {
                        toast.error(e?.message || "Could not export PDF");
                      }
                    }}
                  >
                    <FileDown className="h-4 w-4 mr-1" /> PDF
                  </Button>
                )}
                {!isMd && (
                  // PDFs already are PDFs; DOCX downloads as .docx (browser
                  // doesn't render Word natively, so server-side conversion
                  // would be needed for a true DOCX→PDF — out of scope).
                  <a href={data.raw_url} target="_blank" rel="noopener noreferrer">
                    <Button size="sm" variant="outline" className="w-full">
                      <Download className="h-4 w-4 mr-1" /> Download
                    </Button>
                  </a>
                )}
                <Button size="sm" variant="ghost" onClick={onDelete}>
                  <Trash2 className="h-4 w-4 mr-1 text-destructive" /> Delete
                </Button>
              </>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex-1 overflow-auto">
        {isMd ? (
          editing ? (
            <textarea
              value={editBody}
              onChange={(e) => setEditBody(e.target.value)}
              className="w-full h-[60vh] font-mono text-sm bg-muted/30 p-3 rounded border border-border focus:outline-none focus:ring-1 focus:ring-ring"
              placeholder="# Markdown content..."
            />
          ) : (
            <article
              className="prose-sm prose-knowledge max-w-none"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(data.body_markdown) }}
            />
          )
        ) : isPdf ? (
          <iframe
            src={data.raw_url}
            className="w-full h-[70vh] border rounded"
            title={node.title}
          />
        ) : (
          <div className="text-sm text-muted-foreground space-y-2">
            <p>
              Inline preview for <code>{node.content_format}</code> isn't available
              yet — use the Download button above to view the file.
            </p>
            <p className="text-xs">
              {node.original_filename} ({formatBytes(node.file_size_bytes)})
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// New folder / article modals (lightweight inline panels, no Dialog dep)
// ---------------------------------------------------------------------------

function CreateFolderPanel({
  parentId,
  parentLabel,
  onCancel,
  onCreated,
}: {
  parentId: string;
  parentLabel: string;
  onCancel: () => void;
  onCreated: (n: KnowledgeNode) => void;
}) {
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const m = useMutation({
    mutationFn: () =>
      createKnowledgeFolder({ title, parent_id: parentId || undefined, summary }),
    onSuccess: (n) => {
      toast.success("Folder created");
      onCreated(n);
    },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">New Folder in {parentLabel}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <Input
          placeholder="Folder name"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <Input
          placeholder="Description (optional)"
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
        />
        <div className="flex gap-2 justify-end">
          <Button variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={() => m.mutate()}
            disabled={!title.trim() || m.isPending}
          >
            Create
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function CreateArticlePanel({
  parentId,
  parentLabel,
  onCancel,
  onCreated,
}: {
  parentId: string;
  parentLabel: string;
  onCancel: () => void;
  onCreated: (n: KnowledgeNode) => void;
}) {
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState("");
  const [body, setBody] = useState("# New Article\n\nWrite your content here.\n");
  const m = useMutation({
    mutationFn: () =>
      createKnowledgeArticle({
        title,
        parent_id: parentId || undefined,
        summary,
        tags,
        content_md: body,
      }),
    onSuccess: (n) => {
      toast.success("Article created");
      onCreated(n);
    },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">New Markdown Article in {parentLabel}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <Input
          placeholder="Title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <Input
          placeholder="Summary (optional)"
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
        />
        <Input
          placeholder="Tags (comma-separated, optional)"
          value={tags}
          onChange={(e) => setTags(e.target.value)}
        />
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          className="w-full h-64 font-mono text-sm bg-muted/30 p-3 rounded border border-border focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="flex gap-2 justify-end">
          <Button variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={() => m.mutate()}
            disabled={!title.trim() || m.isPending}
          >
            Create
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function UploadArticlePanel({
  parentId,
  parentLabel,
  onCancel,
  onCreated,
}: {
  parentId: string;
  parentLabel: string;
  onCancel: () => void;
  onCreated: (n: KnowledgeNode) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const m = useMutation({
    mutationFn: () => {
      if (!file) throw new Error("Choose a file first");
      return uploadKnowledgeArticle({
        file,
        title: title || file.name,
        parent_id: parentId || undefined,
        summary,
        tags,
      });
    },
    onSuccess: (n) => {
      toast.success("Uploaded");
      onCreated(n);
    },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Upload File to {parentLabel}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        <input
          type="file"
          ref={inputRef}
          accept=".md,.markdown,.pdf,.docx"
          onChange={(e) => {
            const f = e.target.files?.[0] || null;
            setFile(f);
            if (f && !title) setTitle(f.name.replace(/\.[^.]+$/, ""));
          }}
          className="block text-sm"
        />
        <p className="text-xs text-muted-foreground">
          Supported: .md, .markdown, .pdf, .docx (max 25 MB)
        </p>
        <Input
          placeholder="Title (defaults to filename)"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <Input
          placeholder="Summary (optional)"
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
        />
        <Input
          placeholder="Tags (comma-separated, optional)"
          value={tags}
          onChange={(e) => setTags(e.target.value)}
        />
        <div className="flex gap-2 justify-end">
          <Button variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={() => m.mutate()}
            disabled={!file || m.isPending}
          >
            Upload
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

type ActionMode = null | "folder" | "article" | "upload" | "rename";

function KnowledgePage() {
  const queryClient = useQueryClient();
  const { node: deeplinkNode } = Route.useSearch();
  const navigate = useNavigate();
  const { data: nodes = [], isLoading } = useQuery({
    queryKey: ["knowledgeTree"],
    queryFn: fetchKnowledgeTree,
    staleTime: 10_000,
  });

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<ActionMode>(null);
  const [search, setSearch] = useState("");

  // Deeplink handling: when arriving via `?node=<id>` (e.g. from the use case
  // proposal generator), select that node and expand all of its ancestor
  // folders so it's visible in the tree. Clear the search param afterwards so
  // a manual click on a different node and a refresh don't snap back here.
  const appliedDeeplink = useRef<string | null>(null);
  useEffect(() => {
    if (!deeplinkNode || nodes.length === 0) return;
    if (appliedDeeplink.current === deeplinkNode) return;
    const target = nodes.find((n) => n.node_id === deeplinkNode);
    if (!target) return;
    setSelectedId(deeplinkNode);
    setExpanded((prev) => {
      const next = new Set(prev);
      const byId = new Map(nodes.map((n) => [n.node_id, n] as const));
      let cursor: KnowledgeNode | undefined = target;
      while (cursor?.parent_id) {
        next.add(cursor.parent_id);
        cursor = byId.get(cursor.parent_id);
      }
      return next;
    });
    appliedDeeplink.current = deeplinkNode;
    void navigate({
      search: (prev) => ({ ...prev, node: undefined }),
      replace: true,
    });
  }, [deeplinkNode, nodes, navigate]);

  const tree = useMemo(() => buildTree(nodes), [nodes]);
  const selected = useMemo(
    () => nodes.find((n) => n.node_id === selectedId) || null,
    [nodes, selectedId],
  );
  const childCount = useMemo(
    () =>
      nodes.filter((n) => (n.parent_id || "") === (selectedId || "")).length,
    [nodes, selectedId],
  );

  // The "active parent" for create actions: the selected folder, or the
  // selected article's parent, or root.
  const activeParentId = useMemo(() => {
    if (!selected) return "";
    if (selected.node_type === "folder") return selected.node_id;
    return selected.parent_id || "";
  }, [selected]);
  const activeParentLabel = useMemo(() => {
    if (!activeParentId) return "Root";
    const p = nodes.find((n) => n.node_id === activeParentId);
    return p?.title || "Root";
  }, [activeParentId, nodes]);

  const filteredTree = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return tree;
    // Walk the flat list, mark matches and their ancestors as visible.
    const matchedIds = new Set<string>();
    const byId = new Map(nodes.map((n) => [n.node_id, n] as const));
    nodes.forEach((n) => {
      const hay = `${n.title} ${n.summary} ${n.tags.join(",")}`.toLowerCase();
      if (hay.includes(q)) {
        let cur: KnowledgeNode | undefined = n;
        while (cur) {
          matchedIds.add(cur.node_id);
          cur = cur.parent_id ? byId.get(cur.parent_id) : undefined;
        }
      }
    });
    const filtered = nodes.filter((n) => matchedIds.has(n.node_id));
    return buildTree(filtered);
  }, [tree, nodes, search]);

  // Auto-expand search results
  useEffect(() => {
    if (search.trim()) {
      const ids = new Set<string>();
      const walk = (ns: TreeNode[]) => {
        ns.forEach((n) => {
          if (n.node_type === "folder") ids.add(n.node_id);
          walk(n.children);
        });
      };
      walk(filteredTree);
      setExpanded(ids);
    }
  }, [search, filteredTree]);

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteKnowledgeNode(id, false),
    onSuccess: () => {
      toast.success("Deleted");
      setSelectedId(null);
      queryClient.invalidateQueries({ queryKey: ["knowledgeTree"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      updateKnowledgeNode(id, { title }),
    onSuccess: () => {
      toast.success("Renamed");
      setMode(null);
      queryClient.invalidateQueries({ queryKey: ["knowledgeTree"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const handleCreated = (n: KnowledgeNode) => {
    setMode(null);
    queryClient.invalidateQueries({ queryKey: ["knowledgeTree"] });
    if (n.parent_id) {
      setExpanded((prev) => new Set(prev).add(n.parent_id));
    }
    setSelectedId(n.node_id);
  };

  const handleDelete = () => {
    if (!selected) return;
    const isFolder = selected.node_type === "folder";
    const msg = isFolder
      ? `Delete folder "${selected.title}" and all its contents?`
      : `Delete article "${selected.title}"?`;
    if (!confirm(msg)) return;
    deleteMutation.mutate(selected.node_id);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Knowledge</h1>
          <p className="text-sm text-muted-foreground">
            How-tos, definitions, calculations, and reference docs. Articles can
            be linked to schemas, tables, and artifacts (coming soon).
          </p>
        </div>
      </div>

      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 md:col-span-4 lg:col-span-3">
          <Card className="h-full">
            <CardHeader className="pb-2 space-y-2">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm">Library</CardTitle>
                <Badge variant="outline" className="text-xs">
                  {nodes.length} node{nodes.length === 1 ? "" : "s"}
                </Badge>
              </div>
              <div className="relative">
                <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted-foreground" />
                <Input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search…"
                  className="pl-7 h-8 text-sm"
                />
              </div>
              <div className="flex gap-1">
                <Button
                  size="sm"
                  variant="outline"
                  className="flex-1"
                  onClick={() => setMode("folder")}
                  title={`New folder in ${activeParentLabel}`}
                >
                  <FolderPlus className="h-3 w-3" />
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="flex-1"
                  onClick={() => setMode("article")}
                  title={`New article in ${activeParentLabel}`}
                >
                  <FilePlus className="h-3 w-3" />
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="flex-1"
                  onClick={() => setMode("upload")}
                  title={`Upload file to ${activeParentLabel}`}
                >
                  <Upload className="h-3 w-3" />
                </Button>
              </div>
            </CardHeader>
            <CardContent className="overflow-auto max-h-[70vh]">
              {isLoading ? (
                <p className="text-xs text-muted-foreground">Loading…</p>
              ) : filteredTree.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  {search.trim()
                    ? "No matches."
                    : "Empty. Create a folder or article to get started."}
                </p>
              ) : (
                <TreeView
                  nodes={filteredTree}
                  selectedId={selectedId}
                  expanded={expanded}
                  onToggle={toggleExpanded}
                  onSelect={(n) => setSelectedId(n.node_id)}
                />
              )}
            </CardContent>
          </Card>
        </div>

        <div className="col-span-12 md:col-span-8 lg:col-span-9 space-y-4">
          {mode === "folder" && (
            <CreateFolderPanel
              parentId={activeParentId}
              parentLabel={activeParentLabel}
              onCancel={() => setMode(null)}
              onCreated={handleCreated}
            />
          )}
          {mode === "article" && (
            <CreateArticlePanel
              parentId={activeParentId}
              parentLabel={activeParentLabel}
              onCancel={() => setMode(null)}
              onCreated={handleCreated}
            />
          )}
          {mode === "upload" && (
            <UploadArticlePanel
              parentId={activeParentId}
              parentLabel={activeParentLabel}
              onCancel={() => setMode(null)}
              onCreated={handleCreated}
            />
          )}
          {mode === "rename" && selected && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Rename {selected.title}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                <RenameInline
                  initial={selected.title}
                  onCancel={() => setMode(null)}
                  onSubmit={(t) =>
                    renameMutation.mutate({ id: selected.node_id, title: t })
                  }
                />
              </CardContent>
            </Card>
          )}

          {!selected ? (
            <FolderPreview
              node={null}
              childCount={childCount}
              onCreateFolder={() => setMode("folder")}
              onCreateArticle={() => setMode("article")}
              onUpload={() => setMode("upload")}
              onDelete={() => {}}
              onRename={() => {}}
            />
          ) : selected.node_type === "folder" ? (
            <FolderPreview
              node={selected}
              childCount={childCount}
              onCreateFolder={() => setMode("folder")}
              onCreateArticle={() => setMode("article")}
              onUpload={() => setMode("upload")}
              onDelete={handleDelete}
              onRename={() => setMode("rename")}
            />
          ) : (
            <ArticlePreview
              key={selected.node_id}
              nodeId={selected.node_id}
              onDelete={handleDelete}
              onAfterEdit={() =>
                queryClient.invalidateQueries({ queryKey: ["knowledgeTree"] })
              }
            />
          )}
        </div>
      </div>
    </div>
  );
}

function RenameInline({
  initial,
  onCancel,
  onSubmit,
}: {
  initial: string;
  onCancel: () => void;
  onSubmit: (title: string) => void;
}) {
  const [v, setV] = useState(initial);
  return (
    <div className="flex gap-2">
      <Input value={v} onChange={(e) => setV(e.target.value)} />
      <Button size="sm" variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
      <Button size="sm" onClick={() => onSubmit(v.trim())} disabled={!v.trim()}>
        Save
      </Button>
    </div>
  );
}
