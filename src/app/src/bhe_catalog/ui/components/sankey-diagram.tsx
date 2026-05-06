/**
 * Interactive Sankey Diagram component using D3-sankey.
 * Data Sources -> Entities -> Use Cases -> Departments
 * Supports 3-level (no entities) and 4-level (with entities) modes.
 * Gap entities (UNMAPPED sources) are highlighted in red.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import { Badge } from "@/components/ui/badge";

interface SankeyNode {
  id: string;
  name: string;
  category: string;
  level: number;
  color: string;
  metadata?: Record<string, any>;
}

interface SankeyLink {
  source: string;
  target: string;
  value: number;
  color: string;
  relevance?: string;
}

interface SankeyData {
  nodes: SankeyNode[];
  links: SankeyLink[];
  metadata?: Record<string, any>;
}

interface Props {
  data: SankeyData;
  width?: number;
  height?: number;
  isFullscreen?: boolean;
  onNodeClick?: (node: SankeyNode) => void;
  /**
   * Optional column header labels. One per level. When omitted, defaults to
   * the legacy 3- or 4-column lineage labels (Data Sources / [Entities] /
   * Use Cases / Departments). Provide an array sized to the actual numLevels
   * to relabel columns for other flow shapes (e.g. value sankey:
   * ["Data Sources", "Affiliates", "Use Cases"]).
   */
  columnLabels?: string[];
  /**
   * When true (default), shows the built-in metadata footer badges
   * (sources / entities / use cases / departments / gaps). Set to false
   * when the parent renders its own summary to avoid duplicate counts.
   */
  showFooter?: boolean;
}

const LEVEL_COLORS = [
  "hsl(174, 80%, 55%)", // cyan-ish for sources
  "hsl(270, 60%, 55%)", // violet for entities
  "hsl(250, 70%, 65%)", // purple for use cases
  "hsl(350, 75%, 60%)", // coral for departments
];

const GAP_COLOR = "hsl(0, 75%, 55%)";

const LINK_COLORS: Record<string, string> = {
  Primary: "rgba(120, 200, 180, 0.35)",
  Secondary: "rgba(160, 140, 220, 0.25)",
  Supporting: "rgba(180, 180, 180, 0.18)",
};

export default function SankeyDiagram({
  data,
  width: propWidth,
  height: propHeight,
  isFullscreen = false,
  onNodeClick,
  columnLabels,
  showFooter = true,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [tooltip, setTooltip] = useState<{
    x: number;
    y: number;
    content: string;
  } | null>(null);
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);
  const [containerWidth, setContainerWidth] = useState(propWidth || 1000);

  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect;
      setContainerWidth(propWidth || Math.max(width, 600));
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, [propWidth]);

  const maxLevel = data.nodes.reduce((max, n) => Math.max(max, n.level), 0);
  const numLevels = maxLevel + 1;
  const levels: SankeyNode[][] = Array.from({ length: numLevels }, () => []);
  data.nodes.forEach((n) => {
    if (n.level >= 0 && n.level < numLevels) levels[n.level].push(n);
  });
  const maxNodesInColumn = Math.max(...levels.map((l) => l.length), 1);
  const rowHeight = isFullscreen ? 28 : 24;
  const computedHeight =
    propHeight || Math.max(maxNodesInColumn * rowHeight + 80, 400);

  const adjacency = useCallback(() => {
    const fwd = new Map<string, string[]>();
    const rev = new Map<string, string[]>();
    data.links.forEach((l) => {
      if (!fwd.has(l.source)) fwd.set(l.source, []);
      if (!rev.has(l.target)) rev.set(l.target, []);
      fwd.get(l.source)!.push(l.target);
      rev.get(l.target)!.push(l.source);
    });
    return { fwd, rev };
  }, [data.links]);

  const collectPath = useCallback(
    (nodeId: string) => {
      const { fwd, rev } = adjacency();
      const reachableNodes = new Set<string>([nodeId]);
      const queueFwd = [nodeId];
      while (queueFwd.length) {
        const cur = queueFwd.shift()!;
        for (const nxt of fwd.get(cur) || []) {
          if (!reachableNodes.has(nxt)) {
            reachableNodes.add(nxt);
            queueFwd.push(nxt);
          }
        }
      }
      const queueRev = [nodeId];
      while (queueRev.length) {
        const cur = queueRev.shift()!;
        for (const prev of rev.get(cur) || []) {
          if (!reachableNodes.has(prev)) {
            reachableNodes.add(prev);
            queueRev.push(prev);
          }
        }
      }
      const reachableLinks = new Set<string>();
      data.links.forEach((l) => {
        if (reachableNodes.has(l.source) && reachableNodes.has(l.target)) {
          reachableLinks.add(`${l.source}|${l.target}`);
        }
      });
      return { reachableNodes, reachableLinks };
    },
    [adjacency, data.links],
  );

  const renderSankey = useCallback(() => {
    const svg = svgRef.current;
    if (!svg || !data.nodes.length) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const width = containerWidth;
    const height = computedHeight;

    const labelWidthLeft = isFullscreen ? 200 : 160;
    const labelWidthRight = isFullscreen ? 200 : 160;
    const labelWidthMid = isFullscreen ? 200 : 170;
    const margin = {
      top: 44,
      right: labelWidthRight + 10,
      bottom: 20,
      left: labelWidthLeft + 10,
    };
    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;

    svg.setAttribute("width", String(width));
    svg.setAttribute("height", String(height));
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

    const nodeWidth = isFullscreen ? 16 : 12;
    const nodePadding = isFullscreen ? 4 : 3;
    const levelX: number[] = [];
    for (let i = 0; i < numLevels; i++) {
      levelX.push(
        margin.left + (innerW - nodeWidth) * (i / Math.max(numLevels - 1, 1)),
      );
    }

    const nodePositions = new Map<
      string,
      { x: number; y: number; h: number; node: SankeyNode }
    >();

    levels.forEach((levelNodes, lvl) => {
      const totalValue = levelNodes.reduce((sum, n) => {
        const val = data.links
          .filter((l) => l.source === n.id || l.target === n.id)
          .reduce((s, l) => s + l.value, 0);
        return sum + Math.max(val, 1);
      }, 0);

      const availH = innerH - (levelNodes.length - 1) * nodePadding;
      let currentY = margin.top;

      levelNodes.forEach((n) => {
        const nodeVal = data.links
          .filter((l) => l.source === n.id || l.target === n.id)
          .reduce((s, l) => s + l.value, 0);
        const h = Math.max(
          (Math.max(nodeVal, 1) / totalValue) * availH,
          isFullscreen ? 10 : 6,
        );

        nodePositions.set(n.id, { x: levelX[lvl], y: currentY, h, node: n });
        currentY += h + nodePadding;
      });
    });

    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    svg.appendChild(defs);

    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    svg.appendChild(g);

    const CATEGORY_LABELS_3 = ["Data Sources", "Use Cases", "Departments"];
    const CATEGORY_LABELS_4 = [
      "Data Sources",
      "Entities",
      "Use Cases",
      "Departments",
    ];
    const CATEGORY_LABELS =
      columnLabels && columnLabels.length === numLevels
        ? columnLabels
        : numLevels === 4
          ? CATEGORY_LABELS_4
          : CATEGORY_LABELS_3;
    CATEGORY_LABELS.forEach((label, i) => {
      const text = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "text",
      );
      text.setAttribute("x", String(levelX[i] + nodeWidth / 2));
      text.setAttribute("y", String(margin.top - 16));
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("fill", "currentColor");
      text.setAttribute("font-size", isFullscreen ? "14" : "12");
      text.setAttribute("font-weight", "700");
      text.setAttribute("opacity", "0.6");
      text.setAttribute("letter-spacing", "0.5");
      text.textContent = label.toUpperCase();
      g.appendChild(text);
    });

    const linksGroup = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "g",
    );
    g.appendChild(linksGroup);

    const nodesGroup = document.createElementNS(
      "http://www.w3.org/2000/svg",
      "g",
    );
    g.appendChild(nodesGroup);

    data.links.forEach((link) => {
      const src = nodePositions.get(link.source);
      const tgt = nodePositions.get(link.target);
      if (!src || !tgt) return;

      const srcMidY = src.y + src.h / 2;
      const tgtMidY = tgt.y + tgt.h / 2;
      const linkThickness = Math.max(
        Math.min(link.value * (isFullscreen ? 3 : 2), 12),
        1,
      );

      const gradId = `grad-${link.source}-${link.target}`.replace(
        /[^a-zA-Z0-9-]/g,
        "_",
      );
      const grad = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "linearGradient",
      );
      grad.setAttribute("id", gradId);
      grad.setAttribute("gradientUnits", "userSpaceOnUse");
      grad.setAttribute("x1", String(src.x + nodeWidth));
      grad.setAttribute("x2", String(tgt.x));
      const stop1 = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "stop",
      );
      stop1.setAttribute("offset", "0%");
      stop1.setAttribute(
        "stop-color",
        LEVEL_COLORS[src.node.level] || "#888",
      );
      stop1.setAttribute("stop-opacity", "0.3");
      const stop2 = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "stop",
      );
      stop2.setAttribute("offset", "100%");
      stop2.setAttribute(
        "stop-color",
        LEVEL_COLORS[tgt.node.level] || "#888",
      );
      stop2.setAttribute("stop-opacity", "0.3");
      grad.appendChild(stop1);
      grad.appendChild(stop2);
      defs.appendChild(grad);

      const path = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "path",
      );
      const x1 = src.x + nodeWidth;
      const x2 = tgt.x;
      const cx = (x1 + x2) / 2;
      path.setAttribute(
        "d",
        `M${x1},${srcMidY} C${cx},${srcMidY} ${cx},${tgtMidY} ${x2},${tgtMidY}`,
      );
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", `url(#${gradId})`);
      path.setAttribute("stroke-width", String(linkThickness));
      path.setAttribute("opacity", "0.7");
      path.setAttribute("data-source", link.source);
      path.setAttribute("data-target", link.target);
      path.style.transition = "opacity 0.15s";

      path.addEventListener("mouseenter", (e) => {
        path.setAttribute("opacity", "1");
        path.setAttribute("stroke-width", String(linkThickness + 2));
        setTooltip({
          x: (e as MouseEvent).offsetX,
          y: (e as MouseEvent).offsetY - 14,
          content: `${src.node.name} → ${tgt.node.name}${link.relevance ? ` (${link.relevance})` : ""}`,
        });
      });
      path.addEventListener("mouseleave", () => {
        path.setAttribute("opacity", "0.7");
        path.setAttribute("stroke-width", String(linkThickness));
        setTooltip(null);
      });

      linksGroup.appendChild(path);
    });

    const fontSize = isFullscreen ? 12 : 11;
    const maxLabelLen = isFullscreen ? 36 : 28;

    nodePositions.forEach(({ x, y, h, node }) => {
      const nodeGroup = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "g",
      );
      nodeGroup.style.cursor = "pointer";

      const rect = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "rect",
      );
      rect.setAttribute("x", String(x));
      rect.setAttribute("y", String(y));
      rect.setAttribute("width", String(nodeWidth));
      rect.setAttribute("height", String(Math.max(h, 3)));
      const isGapNode = node.metadata?.is_gap === true || node.metadata?.is_gap === "true";
      rect.setAttribute(
        "fill",
        isGapNode ? GAP_COLOR : LEVEL_COLORS[node.level] || node.color,
      );
      rect.setAttribute("rx", "2");
      rect.style.transition = "opacity 0.15s";

      const text = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "text",
      );
      const isLeft = node.level === 0;
      const isRight = node.level === numLevels - 1;
      const labelX = isLeft
        ? x - 6
        : x + nodeWidth + 6;
      text.setAttribute("x", String(labelX));
      text.setAttribute("y", String(y + Math.max(h / 2, 2) + 4));
      text.setAttribute("text-anchor", isLeft ? "end" : "start");
      text.setAttribute("fill", "currentColor");
      text.setAttribute("font-size", String(fontSize));
      text.setAttribute("opacity", "0.85");

      const baseLabel =
        node.name.length > maxLabelLen
          ? node.name.slice(0, maxLabelLen - 1) + "…"
          : node.name;
      text.textContent = baseLabel;

      nodeGroup.appendChild(rect);
      nodeGroup.appendChild(text);

      // Source-level affiliate hint (small subtitle below name)
      const affiliates: string[] = Array.isArray(node.metadata?.affiliates)
        ? (node.metadata?.affiliates as string[])
        : [];
      const schemaCount = (node.metadata?.schema_count as number) || 0;
      if (node.level === 0 && (affiliates.length > 0 || schemaCount > 0)) {
        const sub = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "text",
        );
        const subParts: string[] = [];
        if (affiliates.length === 1) {
          subParts.push(affiliates[0]);
        } else if (affiliates.length > 1) {
          subParts.push(`${affiliates.length} affiliates`);
        }
        if (schemaCount > 0) {
          subParts.push(`${schemaCount} schema${schemaCount === 1 ? "" : "s"}`);
        }
        const subLabel = subParts.join(" · ");
        if (subLabel) {
          sub.setAttribute("x", String(labelX));
          sub.setAttribute("y", String(y + Math.max(h / 2, 2) + 4 + fontSize + 1));
          sub.setAttribute("text-anchor", isLeft ? "end" : "start");
          sub.setAttribute("fill", "currentColor");
          sub.setAttribute("font-size", String(fontSize - 2));
          sub.setAttribute("opacity", "0.45");
          sub.textContent = subLabel;
          nodeGroup.appendChild(sub);
        }
      }

      nodeGroup.addEventListener("mouseenter", (e) => {
        rect.setAttribute("opacity", "0.7");
        setHoveredNode(node.id);
        const tooltipParts = [node.name];
        if (affiliates.length > 0) {
          tooltipParts.push(
            `Affiliates: ${affiliates.slice(0, 4).join(", ")}` +
              (affiliates.length > 4 ? ` +${affiliates.length - 4}` : ""),
          );
        }
        if (node.level === 0 && schemaCount > 0) {
          tooltipParts.push(`Click to view ${schemaCount} schema(s)`);
        }

        // Source-system "unlock potential" hint for the value-readiness
        // Sankey. Backend (/api/value/sankey) populates these fields on
        // every source node; legacy Sankey payloads that don't set them
        // simply skip this branch (numbers will be 0/undefined).
        const meta = (node.metadata || {}) as Record<string, unknown>;
        const isSourceCategory = node.category === "source";
        const unlocksUc = Number(meta.unlocks_uc_count || 0);
        const unlocksValue = Number(meta.unlocks_value_usd || 0);
        const unlocksMust = Number(meta.unlocks_must_have_count || 0);
        if (isSourceCategory && unlocksUc > 0) {
          const isGap =
            meta.is_gap === true ||
            meta.is_gap === "true" ||
            meta.is_present === false;
          const verb = isGap ? "Ingest to unlock" : "Supports";
          const valueLabel =
            unlocksValue >= 1e9
              ? `$${(unlocksValue / 1e9).toFixed(1)}B`
              : unlocksValue >= 1e6
                ? `$${(unlocksValue / 1e6).toFixed(1)}M`
                : unlocksValue >= 1e3
                  ? `$${(unlocksValue / 1e3).toFixed(0)}K`
                  : `$${Math.round(unlocksValue)}`;
          const mustLabel = unlocksMust > 0 ? ` (${unlocksMust} must-have)` : "";
          tooltipParts.push(
            `${verb} ${unlocksUc} UC${unlocksUc === 1 ? "" : "s"}${mustLabel} · ${valueLabel}`,
          );
        }

        // Use-case node summary: readiness % + bucket + $ value. The
        // value-readiness Sankey sets `readiness_pct`, `readiness_bucket`,
        // and `value_usd` on UC nodes; absent on legacy Sankey payloads.
        if (node.category === "use_case") {
          const pct = meta.readiness_pct;
          const bucket = (meta.readiness_bucket as string | undefined) || "";
          const value = Number(meta.value_usd || 0);
          const bits: string[] = [];
          if (typeof pct === "number") {
            bits.push(`${pct.toFixed(0)}% ready`);
          }
          if (bucket && bucket !== "ready") {
            bits.push(bucket.replace(/_/g, " "));
          }
          if (value > 0) {
            bits.push(
              value >= 1e6
                ? `$${(value / 1e6).toFixed(1)}M`
                : value >= 1e3
                  ? `$${(value / 1e3).toFixed(0)}K`
                  : `$${Math.round(value)}`,
            );
          }
          if (bits.length > 0) {
            tooltipParts.push(bits.join(" · "));
          }
        }
        setTooltip({
          x: (e as MouseEvent).offsetX,
          y: (e as MouseEvent).offsetY - 14,
          content: tooltipParts.join(" · "),
        });

        const { reachableNodes, reachableLinks } = collectPath(node.id);
        linksGroup.querySelectorAll("path").forEach((p) => {
          const ls = p.getAttribute("data-source") || "";
          const lt = p.getAttribute("data-target") || "";
          const inPath = reachableLinks.has(`${ls}|${lt}`);
          p.setAttribute("opacity", inPath ? "1" : "0.04");
        });
        nodesGroup.querySelectorAll("g[data-node-id]").forEach((nGroup) => {
          const nid = nGroup.getAttribute("data-node-id") || "";
          const inPath = reachableNodes.has(nid);
          (nGroup as SVGGElement).style.opacity = inPath ? "1" : "0.12";
        });
      });
      nodeGroup.addEventListener("mouseleave", () => {
        rect.setAttribute("opacity", "1");
        setHoveredNode(null);
        setTooltip(null);
        linksGroup.querySelectorAll("path").forEach((p) => {
          p.setAttribute("opacity", "0.7");
        });
        nodesGroup.querySelectorAll("g[data-node-id]").forEach((nGroup) => {
          (nGroup as SVGGElement).style.opacity = "1";
        });
      });
      nodeGroup.addEventListener("click", () => onNodeClick?.(node));
      nodeGroup.setAttribute("data-node-id", node.id);

      nodesGroup.appendChild(nodeGroup);
    });
  }, [data, containerWidth, computedHeight, isFullscreen, onNodeClick, collectPath, columnLabels]);

  useEffect(() => {
    renderSankey();
  }, [renderSankey]);

  return (
    <div ref={containerRef} className="relative w-full" style={{ minHeight: computedHeight }}>
      <svg
        ref={svgRef}
        className="w-full"
        style={{ height: computedHeight }}
      />
      {tooltip && (
        <div
          className="absolute pointer-events-none z-50 px-3 py-1.5 text-xs bg-popover text-popover-foreground border rounded-lg shadow-lg max-w-xs"
          style={{ left: tooltip.x + 12, top: tooltip.y }}
        >
          {tooltip.content}
        </div>
      )}
      {showFooter && data.metadata && (
        <div className="absolute bottom-2 right-2 flex gap-2">
          <Badge variant="outline" className="text-[10px]">
            {data.metadata.total_sources || 0} sources
          </Badge>
          {(data.metadata.total_entities || 0) > 0 && (
            <Badge variant="outline" className="text-[10px]">
              {data.metadata.total_entities} entities
            </Badge>
          )}
          <Badge variant="outline" className="text-[10px]">
            {data.metadata.total_use_cases || 0} use cases
          </Badge>
          <Badge variant="outline" className="text-[10px]">
            {data.metadata.total_departments || 0} departments
          </Badge>
          {(data.metadata.gap_count || 0) > 0 && (
            <Badge variant="destructive" className="text-[10px]">
              {data.metadata.gap_count} gaps
            </Badge>
          )}
        </div>
      )}
    </div>
  );
}
