from typing import Protocol, runtime_checkable

from ..schemas.contracts import AgentResult, AssessmentRow


@runtime_checkable
class SummaryGenerator(Protocol):
    def generate(
        self,
        results: dict[str, list[AgentResult | None]],
        document_title: str,
        section_labels: dict[str, str],
        agent_type_order: list[str],
        max_priority_actions: int = 10,
    ) -> str: ...


class MarkdownReportGenerator:
    # Grey = Not Applicable; excluded from score % but counted for transparency.
    _RATING_EMOJI = {"Green": "🟢", "Amber": "🟡", "Red": "🔴", "Grey": "⚫"}

    def generate(
        self,
        results: dict[str, list[AgentResult | None]],
        document_title: str,
        section_labels: dict[str, str],
        agent_type_order: list[str],
        max_priority_actions: int = 10,
    ) -> str:
        lines: list[str] = [f"# {document_title}", ""]
        # Render Final Evaluation Summary first
        lines.extend(
            self._render_final_summary(
                results, section_labels, agent_type_order, max_priority_actions
            )
        )
        lines.append("---")
        lines.append("")
        # Then render detailed category sections
        for agent_type in agent_type_order:
            result_list = [r for r in results.get(agent_type, []) if r is not None]
            if not result_list:
                continue
            label = section_labels.get(agent_type, agent_type.title())
            lines.extend(self._render_category_section(result_list, label))
            lines.append("---")
            lines.append("")
        return "\n".join(lines)

    def _render_category_section(
        self,
        results: list[AgentResult],
        label: str,
    ) -> list[str]:
        lines: list[str] = [f"## {label}", ""]
        for result in results:
            for doc in result.docs:
                lines.append(f"### [{doc.policy_doc_filename}]({doc.policy_doc_url})")
                lines.append("")
                lines.append("| Question | Rating | Comments | Reference |")
                lines.append("|---|---|---|---|")
                for row in doc.assessments:
                    emoji = self._RATING_EMOJI.get(row.Rating, "")
                    q = row.Question.replace("|", "\\|")
                    c = row.Comments.replace("|", "\\|")
                    lines.append(
                        f"| {q} | {emoji} {row.Rating} | {c} | {row.Reference} |"
                    )
                lines.append("")
                lines.append("**Summary**")
                lines.append(
                    f"{doc.summary.Interpretation} — {doc.summary.Overall_Comments}"
                )
                lines.append("")
        return lines

    def _render_final_summary(
        self,
        results: dict[str, list[AgentResult | None]],
        section_labels: dict[str, str],
        agent_type_order: list[str],
        max_priority_actions: int,
    ) -> list[str]:
        lines: list[str] = ["## Final Evaluation Summary", ""]
        total_g = total_a = total_r = total_n = 0
        # category_score[label] = % green (lower = worse); Grey excluded from denominator
        category_score: dict[str, int] = {}
        # category_counts[label] = (green, amber, red, grey) for the scorecard
        category_counts: dict[str, tuple[int, int, int, int]] = {}
        for agent_type in agent_type_order:
            result_list = [r for r in results.get(agent_type, []) if r is not None]
            if not result_list:
                continue
            label = section_labels.get(agent_type, agent_type.title())
            g, a, r, n = self._count_ratings(result_list)
            applicable = g + a + r
            score = round((g / applicable) * 100) if applicable > 0 else 0
            category_score[label] = score
            category_counts[label] = (g, a, r, n)
            total_g += g
            total_a += a
            total_r += r
            total_n += n
        applicable_all = total_g + total_a + total_r
        overall_score = (
            round((total_g / applicable_all) * 100) if applicable_all > 0 else 0
        )

        # ── Overall Conclusion ────────────────────────────────────────────────
        lines.append("### Overall Conclusion")
        lines.append("")
        risk = self._classify_risk(overall_score)
        weakest = self._weakest_category(results, section_labels, agent_type_order)
        top = self._top_finding(results, agent_type_order)
        n_categories = sum(
            1
            for at in agent_type_order
            if any(r is not None for r in results.get(at, []))
        )
        na_clause = f", {total_n} N/A excluded" if total_n else ""
        lines.append(
            f"The assessment reviewed {applicable_all} applicable controls across {n_categories} policy areas. "
            f"Overall compliance stands at **{overall_score}% — {risk}** "
            f"({total_g} Green, {total_a} Amber, {total_r} Red{na_clause}). "
            f"{weakest} is the weakest area with {total_r} critical gap(s). "
            f'Most urgent finding: *"{top}"* — immediate remediation required.'
        )
        lines.append("")

        # ── Scorecard ─────────────────────────────────────────────────────────
        lines.append("### Cross-Category Scorecard")
        lines.append("")
        lines.append("| Category | 🟢 Green | 🟡 Amber | 🔴 Red | ⚫ N/A | Score |")
        lines.append("|---|---|---|---|---|---|")
        for label, (g, a, r, n) in category_counts.items():
            score = category_score[label]
            lines.append(f"| {label} | {g} | {a} | {r} | {n} | {score}% |")
        lines.append(
            f"| **Overall** | **{total_g}** | **{total_a}** | **{total_r}** | **{total_n}** | **{overall_score}%** |"
        )
        lines.append("")

        # ── Priority Actions ──────────────────────────────────────────────────
        # Collect all Red/Amber findings, then sort:
        #   1. Red before Amber
        #   2. Within each tier, worst category first (lowest % green score)
        #   3. Within same category+tier, preserve original question order
        priority: list[tuple[str, AssessmentRow]] = []
        for agent_type in agent_type_order:
            result_list = [r for r in results.get(agent_type, []) if r is not None]
            if not result_list:
                continue
            label = section_labels.get(agent_type, agent_type.title())
            for result in result_list:
                for doc in result.docs:
                    for row in doc.assessments:
                        if row.Rating in ("Red", "Amber"):
                            priority.append((label, row))

        priority.sort(
            key=lambda x: (
                0 if x[1].Rating == "Red" else 1,  # Red before Amber
                category_score.get(x[0], 100),  # worst category first
            )
        )

        total_priority = len(priority)
        capped = priority[:max_priority_actions]
        heading = (
            f"### Priority Actions (showing top {len(capped)} of {total_priority})"
        )
        lines.append(heading)
        lines.append("")
        for i, (label, row) in enumerate(capped, 1):
            emoji = self._RATING_EMOJI.get(row.Rating, "")
            lines.append(
                f"{i}. {emoji} **{label}** — {row.Question} *({row.Reference})*"
            )
        lines.append("")
        return lines

    @staticmethod
    def _count_ratings(result_list: list[AgentResult]) -> tuple[int, int, int, int]:
        g = a = r = n = 0
        for result in result_list:
            for doc in result.docs:
                for row in doc.assessments:
                    if row.Rating == "Green":
                        g += 1
                    elif row.Rating == "Amber":
                        a += 1
                    elif row.Rating == "Red":
                        r += 1
                    elif row.Rating == "Grey":
                        n += 1
        return g, a, r, n

    def _classify_risk(self, overall_score: int) -> str:
        if overall_score >= 80:
            return "Low Risk"
        if overall_score >= 60:
            return "Medium Risk"
        return "High Risk"

    def _weakest_category(
        self,
        results: dict[str, list[AgentResult | None]],
        section_labels: dict[str, str],
        agent_type_order: list[str],
    ) -> str:
        worst_label = ""
        worst_score = -1
        for agent_type in agent_type_order:
            result_list = [r for r in results.get(agent_type, []) if r is not None]
            if not result_list:
                continue
            bad = sum(
                1
                for result in result_list
                for doc in result.docs
                for r in doc.assessments
                if r.Rating in ("Red", "Amber")
            )
            if bad > worst_score:
                worst_score = bad
                worst_label = section_labels.get(agent_type, agent_type.title())
        return worst_label or "Unknown"

    def _top_finding(
        self,
        results: dict[str, list[AgentResult | None]],
        agent_type_order: list[str],
    ) -> str:
        for rating in ("Red", "Amber"):
            for agent_type in agent_type_order:
                result_list = [r for r in results.get(agent_type, []) if r is not None]
                for result in result_list:
                    for doc in result.docs:
                        for row in doc.assessments:
                            if row.Rating == rating:
                                return row.Question
        return "No findings"
