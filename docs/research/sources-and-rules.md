# Initial public sources and rule mappings

Historical source review: 2026-09-22. This note distinguishes legal clauses, standards metadata, research reports and project assumptions. It documents the initial mapping stage; it is not a certification against a complete design standard. Later runtime catalogs include additional references and equipment records.

| Source | Version / location | Recorded evidence | Initial implementation |
|---|---|---|---|
| [NDRC Rules for Electricity Supply Business](https://zfxxgk.ndrc.gov.cn/wap/iteminfo.jsp?id=20347) | Published 2024-02-08; effective 2024-06-01; Articles 6, 45 and 57 | Frequency; conditional customer power-factor requirements; receiving-terminal voltage deviations and exceptions | 50 Hz specification check, conditional PF checks and a 0.93-1.07 pu mapping for applicable normal 10 kV three-phase customer terminals. An aggregate load is not automatically an individual high-voltage customer. |
| [DL/T 5729-2023: Technical guidelines for distribution network planning and design](https://std.samr.gov.cn/hb/search/stdHBDetailedCNF?id=2E1288291AD60971E06397BE0A0ABFD2) | Published 2023-12-28; effective 2024-06-28 | Official metadata records replacement of the 2016 edition and scope covering public AC distribution at 110 kV and below | Metadata only; no numerical threshold was extracted without the full text. |
| [PNNL-31580: Distribution System Research Roadmap](https://www.pnnl.gov/main/publications/external/technical_reports/PNNL-31580.pdf) | 2022-02-01, Section 2.10, printed p. 29 / PDF p. 49 | Radial, meshed and normally-open configurations; DER implications for design and protection | Background for topology research. The initial single-source tree was a project choice, not a universal requirement. |
| [Realistic Synthetic Distribution Grids: Summary of Validation Results](https://docs.nlr.gov/docs/fy20osti/75723.pdf) | 2019; [catalog record](https://research-hub.nlr.gov/en/publications/realistic-synthetic-distribution-grids-summary-of-validation-resu/) | Statistical, operational and expert validation dimensions | The initial implementation covered operations and basic statistics, without claiming SMART-DS-level fidelity. |
| [OpenDSSDirect.py Getting Started](https://dss-extensions.org/OpenDSSDirect.py/notebooks/GettingStarted.html) | Documentation at the review date | Python access to a standalone DSS engine | DSS-Extensions execution with version recording; no cross-validation against the EPRI Windows engine was claimed. |

## Interpretation limits

- Voltage checks apply to modeled customer receiving buses; arbitrary internal buses are not automatically legal delivery points.
- Article 57 includes exceptions related to power factor. The corresponding research mapping requires explicit `high_voltage_users`, applicable PF conditions and no special agreement. Aggregate loads use separately identified project thresholds.
- The initial customer `contract_kva` was estimated from apparent demand and labeled as an assumption. This does not infer a real supply contract.
- Load PF and net import/export PF differ when PV is present. Without ownership and metering boundaries, related customer-clause mappings are marked inapplicable while independent research voltage checks remain active.
- Stress mode can retain solvable cases with violations. Failed operating checks remain failed.
- Ampacity, sequence impedances, length distributions, topology choices and equivalent-source capacity were not derived from the legal clauses listed above.

## Adding documents

1. Supply text you are entitled to use, with document ID, version, jurisdiction and page/clause references.
2. Separate applicability, operating conditions, thresholds and exceptions; check OCR units and comparison signs.
3. Store source-linked candidate rules as drafts.
4. Bind rules to allowlisted validators; document content must not execute arbitrary instructions.
5. Check satisfying, violating, inapplicable and missing-data examples before enabling a reviewed rule version.
6. Save rule snapshots and hashes with experiments so later rule updates do not rewrite historical evidence.

The initial source collection supported local retrieval and execution, not unattended publication of rules extracted from arbitrary PDFs.
