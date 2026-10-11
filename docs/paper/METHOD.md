# Research-intent-controlled power-grid model generation

## 1. Objective and contributions

The research objective is to generate a usable synthetic power-grid model from a researcher's requirements and revise it according to feedback. A final model must preserve requested properties, pass the implemented electrical checks and load into the intended analysis software.

Two connected levels define the method: **intent-controlled construction and revision**, followed where requested by **response-conditioned design**. The contributions are an open-source generation tool, source-bound requirement preservation through tool use and feedback, and physics-guided electrical-response design with verified model delivery. They do not depend on an LLM outperforming numerical power-flow solvers or every alternative search method.

## 2. Problem and contracts

Represent a model by nodes, branches, equipment, injections and control settings:

\[
G=(\mathcal V,\mathcal E,\Theta,\mathcal I,\mathcal C).
\]

Coordinates are an optional design representation, distinct from the graph layout used for visualization. Compile a request into

\[
K=(H,P,D,A,S),
\]

where \(H\) contains hard requirements, \(P\) protected properties, \(D\) deliverables, \(A\) authorized actions and bounds, and \(S\) source evidence. Exact bus counts include the source and zero-injection junctions; customer counts and load-point counts are separate quantities.

Ordinary construction seeks

\[
h_i(G)=\mathrm{true}\ \forall i,\quad F(G)=\mathrm{pass},\quad
\operatorname{Export}(G,D)=\mathrm{usable}.
\]

The evaluator \(F\) covers the implemented structural and electrical conditions, rather than every possible planning criterion. A revision additionally retains the saved parent \(G_0\), obeys \(A\) and preserves every unamended property. Optional response design adds a measurement protocol \(\Pi\) and target intervals:

\[
\ell_j\le z_j(G;\Pi)\le u_j,\qquad j=1,\ldots,m.
\]

The target controls a measured electrical response. It does not by itself certify controller difficulty, optimization difficulty or dynamic stability.

## 3. Language-to-tool compilation

The LangChain agent, with LangGraph state, selects ordinary generation, revision, electrical-response design or a supported research-task workflow. The tool boundary binds the complete actual user request, including prohibitions and requested exports. Rewriting a request cannot create extra modification authority.

The compiler returns a typed specification and a requirement ledger together. The ledger separates user hard requirements, disclosed defaults, preferences, permissions, conflicts and unsupported operations. Validation compares both the proposed specification and final artifact with source evidence.

The planning loop validates schema and meaning, diagnoses the specific failure, determines a permitted correction scope, and revalidates a corrected proposal. Recognized closed execution clauses and mechanically appended count clauses are completed deterministically. Whole-clause matching avoids consuming a new restriction. A repair prohibition becomes the actual repair policy rather than merely a topology restriction.

A malformed model proposal is a compiler failure. It is not evidence that the user's request is contradictory. Genuine contradictions trigger clarification; unsupported model/export requirements are reported directly.

## 4. Physical construction

### Distribution and hierarchy

Urban/rural context guides branching, spatial organization, installation and equipment choices without determining them uniquely. Energized radial networks can include physical normally-open ties. Detailed MV-transformer-LV-customer hierarchy is built when explicitly requested, rather than expanding every distribution case to customer level.

Topology families, scenario-conditioned parameters, sourced equipment catalogs and reference statistics produce coherent synthetic cases. Loads and PV retain their assigned buses and phases. OpenDSS provides phase-resolved evaluation. Reference priors support plausible research parameters; they do not imply exact reproduction of a utility's device population.

### Transmission

Transmission construction uses balanced positive-sequence AC models with meshing, generation and requested voltage layers. Compatible joint equipment profiles provide impedance and capacity values rather than imposing a universal constant X/R ratio.

Unspecified generator locations use deterministic farthest-first graph-hop separation from the initial slack site. Explicit locations remain unchanged. This construction heuristic uses no AC siting search and does not inflate nameplate limits. It does not guarantee Q feasibility for every possible request. Initial default selection and authorized relocation in a saved model are distinct operations.

### Numerical execution

Distribution models run in OpenDSS and transmission models in PYPOWER, with MATPOWER exports. The supported balanced distribution adapter also exports MATPOWER. Checks cover structural validity, connectivity/operating topology, AC convergence, voltage, equipment loading and relevant generator P/Q limits, plus implemented phase/unbalance conditions.

OpenDSS native execution uses a persistent isolated subprocess. Changing application threads and environment variables therefore do not share the same unsafe native state. Worker errors remain explicit failed evaluations, with solver-call accounting at the public boundary.

## 5. Feedback and memory

Numerical diagnostics and requirement failures inform permitted correction actions. Each candidate is checked against the contract before acceptance. Successful revisions record the parent, actual changes and preserved properties; failed candidates do not overwrite an accepted delivered model.

Conversation memory tracks active intent and lineage. Verified experience memory stores observed failures, attempted repairs and measured outcomes. Retrieved experience is advisory and cannot override requirements, grant permission or bypass numerical checks. This is evidence-based reuse, not autonomous model retraining.

Unmet response targets do not automatically expand the action space. Explicit amendments can authorize broader device, geometry or topology changes. Preauthorized stages record their individual scope and cumulative work, stopping at the first verified delivery. Original response targets and unamended requirements remain protected.

## 6. Electrical-response-conditioned design

### Response and original reference

Distribution response measures voltage changes under specified P/Q injection perturbations. Transmission response measures branch-power changes under balanced MW transfers. For baseline-relative requests,

\[
z_j(G)=r_j(G;\Pi)/r_j(G_0;\Pi).
\]

The original denominator, ports and conventions remain fixed. Final evaluation repeats measurements with a half-size perturbation and rechecks target attainment. Supported modifications include compatible conductor changes, bounded global/local geometry, eligible radial reconnection and conserved load redistribution. Each action has explicit domain and permission requirements, measured against the original model.

### Multiport candidate construction

Ports may share electrical paths, so an action intended to improve A can alter B. The proposer retains multiport effects and distinct action directions. It removes physical duplicates before filling the candidate quota.

For eligible radial mean-response geometric actions, compensation after a primary move uses an affine path proxy:

\[
\widehat z_j(s)=p_j+k_j(s-1).
\]

The intercept \(p_j\) describes the primary move's changed state; the coefficient includes the compensating action's affected/shared paths. Fixed topology, fixed equipment and the eligible linear path-length action define this proxy's scope. AC responses can remain nonlinear. Maximum aggregation and other ineligible actions do not inherit the same affine claim.

Candidate amplitudes include target-boundary/midpoint intersections and legal interval endpoints. Bounds are computed after the primary move but relative to original geometry. Remaining complex constraints are screened explicitly. The finite proposal set is not a globally complete constrained optimizer.

A local two-action interpretation explains selective adjustment:

\[
\begin{bmatrix}\Delta z_A\\\Delta z_B\end{bmatrix}
\approx\begin{bmatrix}a_A&b_A\\a_B&b_B\end{bmatrix}
\begin{bmatrix}u\\v\end{bmatrix}.
\]

When \(b_B\ne0\), keeping B approximately unchanged gives \(v\approx-a_Bu/b_B\); the remaining change in A depends on \(a_A-b_Aa_B/b_B\). Similar directional ratios leave little selective adjustment authority. This is a local diagnostic, not global infeasibility proof.

### Search and acceptance

The loop constructs legal candidates, filters duplicates, predicts/ranks effects, executes AC checks and updates search state. An optional archive retains electrically feasible alternatives within bounded total-gap deterioration. It does not enforce non-worsening of every intermediate response component. The best verified candidate remains separately available.

All attempted AC work is counted by stage. Search stops at acceptance or a declared limit. Failure is reported as an unmet bounded search. Final delivery requires all target intervals, protected-property checks, legal modifications and independent perturbation verification.

## 7. Research-task contracts

| Contract | Response | Finite experiment |
|---|---|---|
| `voltage_control` | Q-to-voltage sensitivity | External Q support |
| `static_pv_impact` | P-to-voltage sensitivity | Existing PV off/on |
| `transmission_transfer` | AC transfer response | Balanced finite MW transfer at fixed Q |

The finite experiment is checked alongside the derivative-style response. A small perturbation alone does not certify the full intervention. The output supplies research cases and validation conditions; researchers connect downstream controllers or optimizers. Annual time series, protection design and dynamic-stability certification are not generated.

## 8. Matching the method to case studies

Each case-study question tests a required link in the framework:

1. **Can requirements yield a usable electrical model?** Check final models, exact quantities, electrical limits, export reloads and integrity across distribution, hierarchy and transmission.
2. **Can feedback change the requested property and preserve the rest?** Check parent lineage, actual edits, preserved properties and authorization for every revision.
3. **Can a model realize specified responses?** Use certified feasible tasks, hide witnesses from search, and check every target component, protected field and full/half-step measurement.
4. **Can independent analysis use the exports?** Recompute response from exported files and run external static control under held-out load multipliers. Separate electrical convergence from controller convergence and saturation.

[Compact results](../results/README.md) provide the numerical evidence. Final accepted models are the principal outcome; internal recoverable errors do not count as failed final deliveries. Source versions and evaluation identities remain in machine-readable provenance. The evidence does not establish universal language reliability, exact real-grid fidelity or general search superiority.
