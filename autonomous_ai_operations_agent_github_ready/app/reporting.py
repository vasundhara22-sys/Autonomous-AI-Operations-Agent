"""Report generation: chart (PNG), PDF (ReportLab), Excel and CSV (pandas + openpyxl)."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet  # noqa: E402
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402

REGION_COLUMNS = [
    "region", "manager", "manager_email", "actual", "target", "achievement",
    "gap_to_target", "wow_growth", "status", "suggested_follow_up",
]


def _regions_frame(metrics: dict) -> pd.DataFrame:
    return pd.DataFrame(metrics["regions"]).reindex(columns=REGION_COLUMNS)


def _pct(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:.1%}"


def _money(value) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:,.0f}"


def write_chart(metrics: dict, path: Path) -> Path:
    df = _regions_frame(metrics).dropna(subset=["achievement"]).sort_values("achievement")
    fig, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
    bar_colors = ["#B03A2E" if s == "UNDERPERFORMING" else "#1F6F8B" for s in df["status"]]
    values = df["achievement"] * 100
    ax.barh(df["region"], values, color=bar_colors)
    ax.axvline(metrics["threshold"] * 100, color="#7F8C8D", linestyle="--", label=f"Threshold {metrics['threshold']:.0%}")
    for y, v in enumerate(values):
        ax.text(v + 0.5, y, f"{v:.1f}%", va="center", fontsize=8)
    ax.set_xlim(0, max(110.0, float(values.max()) + 10) if len(values) else 110)
    ax.set_xlabel("Target achievement (%)")
    ax.set_title(f"Regional achievement, week of {metrics['period_week']}")
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def write_pdf(metrics: dict, path: Path, chart_path: Path | None) -> Path:
    styles = getSampleStyleSheet()

    def para(text: str, style: str = "BodyText") -> Paragraph:
        return Paragraph(escape(str(text)), styles[style])

    totals = metrics["totals"]
    story = [
        para("Weekly Sales Performance Report", "Title"),
        para(
            f"Reporting period: {metrics['period_week']} to {metrics['period_end']} "
            f"(comparison week: {metrics['comparison_week']})"
        ),
        para(f"Data sources: {'; '.join(metrics['data_sources'])}"),
        Spacer(1, 8),
        para("Executive summary", "Heading2"),
        *[para(f"- {obs}") for obs in metrics["observations"]],
        para(
            f"Totals (evaluated regions): actual {_money(totals['actual'])}, target {_money(totals['target'])}, "
            f"achievement {_pct(totals['achievement'])}."
        ),
        Spacer(1, 8),
        para("Regional performance", "Heading2"),
    ]

    header = ["Region", "Manager", "Actual", "Target", "Achievement", "WoW", "Status"]
    rows = [header]
    for r in metrics["regions"]:
        rows.append([
            r["region"], r["manager"] or "n/a", _money(r["actual"]), _money(r["target"]),
            _pct(r["achievement"]), _pct(r["wow_growth"]), r["status"],
        ])
    table = Table(rows, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C4D6")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (2, 1), (5, -1), "RIGHT"),
    ]))
    story.append(table)
    story.append(Spacer(1, 10))
    if chart_path and Path(chart_path).exists():
        story.append(Image(str(chart_path), width=470, height=247))

    story.append(para("Underperforming regions", "Heading2"))
    if metrics["underperformers"]:
        for r in metrics["underperformers"]:
            story.append(para(f"- {r['region']} ({r['manager'] or 'no manager on file'}): "
                              f"{_pct(r['achievement'])} of target. {r['suggested_follow_up']}"))
    else:
        story.append(para("No evaluated region is below the threshold."))

    story.append(para("Recommended actions", "Heading2"))
    for rec in metrics["recommendations"]:
        story.append(para(f"- {rec}"))

    story.append(para("Methodology", "Heading2"))
    story.append(para(metrics["methodology"]))

    story.append(para("Data limitations", "Heading2"))
    if metrics["data_gaps"]:
        for gap in metrics["data_gaps"]:
            story.append(para(f"- {gap['region']}: {gap['issue']}"))
    else:
        story.append(para("No missing actual values for this period."))

    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36,
        title="Weekly Sales Performance Report",
    )
    doc.build(story)
    return path


def write_excel(metrics: dict, path: Path) -> Path:
    totals = metrics["totals"]
    summary = pd.DataFrame(
        [
            ("Reporting period", f"{metrics['period_week']} to {metrics['period_end']}"),
            ("Comparison week", metrics["comparison_week"]),
            ("Threshold", f"{metrics['threshold']:.0%}"),
            ("Total actual (evaluated)", totals["actual"]),
            ("Total target (evaluated)", totals["target"]),
            ("Overall achievement", totals["achievement"]),
            ("Regions evaluated", totals["regions_evaluated"]),
            ("Regions underperforming", len(metrics["underperformers"])),
            ("Methodology", metrics["methodology"]),
        ],
        columns=["Metric", "Value"],
    )
    regional = _regions_frame(metrics)
    underperforming = regional[regional["status"] == "UNDERPERFORMING"]
    gaps = pd.DataFrame(metrics["data_gaps"], columns=["region", "issue"])
    trend = pd.DataFrame(metrics["trend"])

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        regional.to_excel(writer, sheet_name="Regional", index=False)
        underperforming.to_excel(writer, sheet_name="Underperforming", index=False)
        trend.to_excel(writer, sheet_name="Trend", index=False)
        gaps.to_excel(writer, sheet_name="Data limitations", index=False)
        for sheet in writer.book.worksheets:
            for column in sheet.columns:
                width = max(len(str(cell.value or "")) for cell in column)
                sheet.column_dimensions[column[0].column_letter].width = min(max(width + 2, 12), 60)
    return path


def write_csv(metrics: dict, path: Path) -> Path:
    _regions_frame(metrics).to_csv(path, index=False)
    return path
