# Synapse Synthetic Healthcare Domain and Golden Scenario Design

## 1. Purpose, scope, and safety boundary

This document defines the Milestone 1B synthetic domain that will later drive the five mock source adapters, deterministic reconciliation, confidence and escalation policies, governed correction workflow, audit history, and golden integration tests. It is a design artifact only: it does not define database tables, create fixtures, or implement application behavior.

Everything below is fictional. Names, identifiers, organizations, medications, conditions, policies, notes, and excerpts were invented for this prototype and must be displayed with a visible **SYNTHETIC — DEMO ONLY** label. They are not medical or coverage facts and must not be used for real care or insurance decisions.

The world contains one primary case, five logical source documents (one for each approved adapter), two immutable versions of the payer-policy document, and one scenario-only formulary variant used solely to test a genuine contradiction. The normal path remains intentionally small enough to explain in under 30 seconds.

### Design acceptance criteria

- The canonical question can be answered deterministically from all five source categories without an LLM, network access, or lexical search.
- Every material answer claim maps to one or more identified evidence items.
- Clinical support and payer authorization requirements are represented as different decision dimensions and classified as a compatible constraint, not a contradiction.
- The genuine contradiction compares the same payer, plan, medication, indication, effective instant, and authorization decision dimension.
- A missing critical payer policy yields no inferred policy content, `LOW` confidence, and mandatory human escalation.
- Payer Policy V2 remains noncurrent while pending and becomes current only after atomic approval/application and its effective date.
- V1, its evidence, the original answer snapshot, and all governance events remain immutable after V2 becomes current.

## 2. Synthetic-world overview

The visibly fictional **Aurora Vale Care Network** coordinates care for case `SYN-CASE-001`. The case concerns **Veluntra**, an invented medication requested for **Lumen Drift Syndrome**, an invented recurring condition. The patient is enrolled in the fictional **Harborlight Plus** plan offered by **Northstar Harbor Health**.

The patient has already tried the invented preferred medication **Norlaxa**, which was stopped because the documented response was inadequate. A synthetic guideline supports Veluntra after one documented preferred-therapy failure. The initial payer policy, V1, still requires two preferred therapies—Norlaxa and the invented medication **Bravex**—before authorization review. The formulary lists Veluntra as covered on Tier 3 with prior authorization, and a specialist note documents the request and the Norlaxa history.

The judge-facing summary is therefore:

> Synthetic case `SYN-CASE-001` requests fictional Veluntra for fictional Lumen Drift Syndrome. The clinical guideline supports the request after the documented Norlaxa failure, but the current payer policy requires prior authorization and, in V1, another prerequisite. Synapse shows both dimensions, cites the evidence, and gives the next operational step without claiming approval.

## 3. Primary synthetic patient journey

| Element | Exact proposed value |
|---|---|
| Case | `SYN-CASE-001`, display label **Synthetic Case Aurora-001** |
| Patient | `SYN-PAT-001`, display label **Synthetic Patient A-001**; no person-like name, birth date, address, or realistic member number |
| Condition | `SYN-COND-LDS`, **Lumen Drift Syndrome** (invented), status active, documented `2026-02-03` |
| Requested medication | `SYN-MED-VEL`, **Veluntra** (invented), request documented `2026-06-10` |
| Prior therapy | `SYN-MED-NOR`, **Norlaxa** (invented), completed for 35 days from `2026-03-01` through `2026-04-04`; inadequate response documented; no Bravex trial documented |
| Payer | `SYN-PAYER-NHH`, **Northstar Harbor Health** (invented) |
| Plan | `SYN-PLAN-HLP`, **Harborlight Plus** (invented), active from `2026-01-01` through `2026-12-31` |
| Guideline | **Aurora Vale Lumen Drift Care Guide**, version `2026.1` |
| Payer policy | **Northstar Harbor Veluntra Authorization Policy**, V1 initially current; V2 pre-seeded but not initially authoritative |
| Formulary | **Harborlight Plus Synthetic Formulary**, version `2026-H1` |
| Specialist note | **Synthetic Specialist Consultation — Lumen Drift**, version `1` |

### Journey sequence

1. On `2026-06-15T14:00:00Z`, a care coordinator selects `SYN-CASE-001` and asks the canonical prior-authorization question.
2. All five adapters return current, applicable evidence. Synapse states that prior authorization is required under Payer Policy V1, that V1's two-agent prerequisite is not yet satisfied, and that the guideline independently supports Veluntra clinically after the documented Norlaxa failure.
3. On `2026-06-16T15:00:00Z`, the care coordinator flags V1 as outdated and selects the pre-seeded V2 document version. The validated correction becomes `PENDING`; V1 remains current.
4. On `2026-07-02T13:00:00Z`, a logically distinct synthetic `KNOWLEDGE_REVIEWER` approves the correction. Approval and application commit atomically. V2 was effective from `2026-07-01`, so it now becomes the governed current version; V1 is linked as superseded but is not changed or deleted.
5. On `2026-07-03T14:00:00Z`, a new canonical question retrieves V2. Synapse still states that prior authorization is required, but now states that the one-agent prerequisite is met and the coordinator can prepare the authorization request with the documented evidence.
6. Reopening the `2026-06-15` interaction still shows the immutable V1 evidence and original answer snapshot.

## 4. Source inventory

There are exactly five adapter categories and five logical source documents in the primary world. Document versions and evidence excerpts have identities separate from the logical document.

| Adapter | Source system ID / `source_type` | Logical document ID and `source_title` | Primary version(s) | Role in the answer |
|---|---|---|---|---|
| EHR | `SRC-SYN-EHR` / `EHR` | `DOC-SYN-EHR-CASE-001` / `SYNTHETIC — Aurora Vale Case Summary: SYN-CASE-001` | `DV-SYN-EHR-CASE-001-V1` | Establishes case, condition, medication request, plan, and prior therapy |
| Guideline | `SRC-SYN-GUIDE` / `GUIDELINE` | `DOC-SYN-GUIDE-LDS` / `SYNTHETIC — Aurora Vale Lumen Drift Care Guide` | `DV-SYN-GUIDE-LDS-2026-1` | Supports the medication on the clinical-appropriateness dimension |
| Payer Policy | `SRC-SYN-PAYER` / `PAYER_POLICY` | `DOC-SYN-POL-VEL` / `SYNTHETIC — Northstar Harbor Veluntra Authorization Policy` | `DV-SYN-POL-VEL-V1`, `DV-SYN-POL-VEL-V2` | Determines prior-authorization and prerequisite rules for the plan |
| Formulary | `SRC-SYN-FORM` / `FORMULARY` | `DOC-SYN-FORM-HLP` / `SYNTHETIC — Harborlight Plus Synthetic Formulary` | `DV-SYN-FORM-HLP-2026-H1`; scenario-only `DV-SYN-FORM-HLP-CONFLICT` | Establishes formulary status, tier, and restriction; conflict variant tests same-dimension disagreement |
| Specialist Notes | `SRC-SYN-NOTE` / `SPECIALIST_NOTE` | `DOC-SYN-NOTE-001` / `SYNTHETIC — Specialist Consultation: Lumen Drift` | `DV-SYN-NOTE-001-V1` | Adds dated specialist history and request context |

All five are local mocked sources behind explicit adapters. No document represents or imitates a real EHR, insurer, formulary, guideline publisher, or clinician.

## 5. Exact proposed synthetic source content

### 5.1 EHR document

**Display title:** `SYNTHETIC — Aurora Vale Case Summary: SYN-CASE-001`

**Exact proposed content:**

> Synthetic Patient A-001 has active fictional Lumen Drift Syndrome, documented 2026-02-03. Veluntra was requested on 2026-06-10. Harborlight Plus coverage is active from 2026-01-01 through 2026-12-31. Norlaxa was used from 2026-03-01 through 2026-04-04 and stopped after an inadequate response. No Bravex trial is documented in this synthetic record.

**Structured fields/tags:**

- `case_id=SYN-CASE-001`
- `patient_id=SYN-PAT-001`
- `condition_id=SYN-COND-LDS`
- `requested_medication_id=SYN-MED-VEL`
- `payer_id=SYN-PAYER-NHH`
- `plan_id=SYN-PLAN-HLP`
- `coverage_start=2026-01-01`
- `coverage_end=2026-12-31`
- `prior_therapy_medication_id=SYN-MED-NOR`
- `prior_therapy_days=35`
- `prior_therapy_outcome=INADEQUATE_RESPONSE`
- `bravex_trial_status=NOT_DOCUMENTED`
- `synthetic=true`

**Evidence and assertions:**

| Evidence ID | Section/location | Relevant excerpt | Supported assertions |
|---|---|---|---|
| `EV-SYN-EHR-CONTEXT-001` | `case-summary/condition-request` | “Synthetic Patient A-001 has active fictional Lumen Drift Syndrome… Veluntra was requested on 2026-06-10.” | Patient has condition; patient has requested medication |
| `EV-SYN-EHR-PLAN-001` | `case-summary/coverage` | “Harborlight Plus coverage is active from 2026-01-01 through 2026-12-31.” | Patient has applicable payer/plan during the question time |
| `EV-SYN-EHR-THERAPY-001` | `case-summary/prior-therapy` | “Norlaxa was used… for 35 days… inadequate response. No Bravex trial is documented…” | One preferred therapy failed; a Bravex trial is not established |

### 5.2 Guideline document

**Display title:** `SYNTHETIC — Aurora Vale Lumen Drift Care Guide`

**Exact proposed content:**

> For the fictional condition Lumen Drift Syndrome, Veluntra is a supported clinical option when one preferred therapy has produced an inadequate response. This synthetic recommendation addresses clinical appropriateness only. It does not determine insurance coverage, authorization, or payment.

**Structured fields/tags:**

- `condition_id=SYN-COND-LDS`
- `medication_id=SYN-MED-VEL`
- `minimum_preferred_failures=1`
- `decision_dimension=CLINICAL_APPROPRIATENESS`
- `recommendation=SUPPORTED_OPTION`
- `coverage_authority=false`
- `synthetic=true`

**Evidence and assertions:**

| Evidence ID | Section/location | Relevant excerpt | Supported assertions |
|---|---|---|---|
| `EV-SYN-GUIDE-SUPPORT-001` | `recommendations/veluntra` | “Veluntra is a supported clinical option when one preferred therapy has produced an inadequate response.” | Guideline supports Veluntra clinically for the condition after one failure |
| `EV-SYN-GUIDE-SCOPE-001` | `limitations/coverage` | “This synthetic recommendation addresses clinical appropriateness only. It does not determine insurance coverage, authorization, or payment.” | Guideline has no coverage-authority assertion |

### 5.3 Payer-policy document

The logical document has two immutable versions. Section 6 defines their exact difference and governance state.

**V1 exact proposed content:**

> Harborlight Plus requires prior authorization for Veluntra when requested for fictional Lumen Drift Syndrome. Before review, the submitted record must document at least 30 days of both Norlaxa and Bravex, with an inadequate response or documented intolerance for each. Submit the condition, requested medication, therapy dates, and outcomes. This policy does not determine clinical appropriateness and does not guarantee coverage approval.

**V2 exact proposed content:**

> Harborlight Plus requires prior authorization for Veluntra when requested for fictional Lumen Drift Syndrome. Before review, the submitted record must document at least 30 days of either Norlaxa or Bravex, with an inadequate response or documented intolerance. Submit the condition, requested medication, therapy dates, and outcome. This policy does not determine clinical appropriateness and does not guarantee coverage approval.

**Shared structured fields/tags:**

- `payer_id=SYN-PAYER-NHH`
- `plan_id=SYN-PLAN-HLP`
- `condition_id=SYN-COND-LDS`
- `medication_id=SYN-MED-VEL`
- `prior_authorization_required=true`
- `decision_dimension=AUTHORIZATION_REQUIREMENT`
- `synthetic=true`

**Version-specific structured fields:**

| Field | V1 | V2 |
|---|---|---|
| `required_preferred_therapies` | `[SYN-MED-NOR, SYN-MED-BRV]` | `[SYN-MED-NOR, SYN-MED-BRV]` |
| `minimum_distinct_failures` | `2` | `1` |
| `combination_rule` | `ALL` | `ANY` |
| `minimum_days_each` | `30` | `30` |
| `governance_state_at_seed` | `APPLIED` | `CANDIDATE_NOT_CURRENT` |

**Evidence and assertions:**

| Evidence ID | Version | Section/location | Relevant excerpt | Supported assertions |
|---|---|---|---|---|
| `EV-SYN-POL-V1-PA-001` | V1 | `coverage/prior-authorization` | “Harborlight Plus requires prior authorization for Veluntra…” | PA is required for the normalized plan/medication/indication scope |
| `EV-SYN-POL-V1-STEP-001` | V1 | `criteria/prior-therapy` | “at least 30 days of both Norlaxa and Bravex…” | V1 requires two named therapy failures |
| `EV-SYN-POL-V2-PA-001` | V2 | `coverage/prior-authorization` | “Harborlight Plus requires prior authorization for Veluntra…” | PA remains required for the same normalized scope |
| `EV-SYN-POL-V2-STEP-001` | V2 | `criteria/prior-therapy` | “at least 30 days of either Norlaxa or Bravex…” | V2 requires one of the named therapy failures |

### 5.4 Formulary document

**Baseline exact proposed content:**

> For Harborlight Plus during 2026 H1, fictional Veluntra is listed as Tier 3 and covered subject to prior authorization. Norlaxa and Bravex are listed as preferred prerequisite options. Formulary listing does not establish clinical appropriateness or guarantee coverage approval.

**Scenario-only conflict variant content:**

> For Harborlight Plus during 2026 H1, fictional Veluntra is listed as Tier 3 and covered with no prior authorization restriction for Lumen Drift Syndrome.

The conflict variant is never current in the baseline or correction demo. It is activated only in Golden Scenario F so the system can prove that it detects rather than hides a true same-dimension disagreement.

**Structured fields/tags:**

- `plan_id=SYN-PLAN-HLP`
- `payer_id=SYN-PAYER-NHH`
- `medication_id=SYN-MED-VEL`
- `condition_id=SYN-COND-LDS`
- `tier=3`
- baseline `prior_authorization_required=true`
- conflict variant `prior_authorization_required=false`
- `decision_dimension=AUTHORIZATION_REQUIREMENT` for the restriction assertion
- `preferred_options=[SYN-MED-NOR, SYN-MED-BRV]`
- `synthetic=true`

**Evidence and assertions:**

| Evidence ID | Version | Section/location | Relevant excerpt | Supported assertions |
|---|---|---|---|---|
| `EV-SYN-FORM-STATUS-001` | `2026-H1` | `entries/veluntra` | “Veluntra is listed as Tier 3 and covered subject to prior authorization.” | Veluntra is Tier 3; baseline formulary agrees PA is required |
| `EV-SYN-FORM-PREF-001` | `2026-H1` | `entries/preferred-options` | “Norlaxa and Bravex are listed as preferred prerequisite options.” | Norlaxa and Bravex are preferred options |
| `EV-SYN-FORM-CONFLICT-001` | `2026-H1-CONFLICT` | `entries/veluntra` | “Veluntra… [is] covered with no prior authorization restriction for Lumen Drift Syndrome.” | Scenario F asserts PA is not required for the same scope |

### 5.5 Specialist-note document

**Display title:** `SYNTHETIC — Specialist Consultation: Lumen Drift`

**Exact proposed content:**

> Synthetic consultation for case SYN-CASE-001: Lumen Drift Syndrome remains active. Norlaxa was used for 35 days with inadequate response. Veluntra is requested as the next clinical option. Authorization requirements should be verified against the patient's current synthetic plan; this note does not establish coverage.

**Structured fields/tags:**

- `case_id=SYN-CASE-001`
- `specialty=SYNTHETIC_LUMEN_SERVICE`
- `condition_id=SYN-COND-LDS`
- `requested_medication_id=SYN-MED-VEL`
- `prior_therapy_medication_id=SYN-MED-NOR`
- `prior_therapy_outcome=INADEQUATE_RESPONSE`
- `coverage_authority=false`
- `synthetic=true`

**Evidence and assertions:**

| Evidence ID | Section/location | Relevant excerpt | Supported assertions |
|---|---|---|---|
| `EV-SYN-NOTE-HISTORY-001` | `assessment/history` | “Norlaxa was used for 35 days with inadequate response.” | Specialist note corroborates the prior-therapy history |
| `EV-SYN-NOTE-REQUEST-001` | `plan/request` | “Veluntra is requested as the next clinical option.” | Specialist requested Veluntra; note does not decide coverage |

## 6. Structured source metadata and V1/V2 design

### 6.1 Document-version metadata

| Document version ID | Version label | Source-issued `timestamp` | `recorded_at` | `effective_from` | `effective_to` | Initial authority state |
|---|---|---|---|---|---|---|
| `DV-SYN-EHR-CASE-001-V1` | `1` | `2026-06-10T16:00:00Z` | `2026-06-10T16:05:00Z` | `2026-06-10T16:00:00Z` | none | Current case snapshot |
| `DV-SYN-GUIDE-LDS-2026-1` | `2026.1` | `2026-01-05T12:00:00Z` | `2026-01-05T12:05:00Z` | `2026-01-01T00:00:00Z` | `2026-12-31T23:59:59Z` | Current |
| `DV-SYN-POL-VEL-V1` | `1.0` | `2025-12-15T12:00:00Z` | `2025-12-15T12:05:00Z` | `2026-01-01T00:00:00Z` | `2026-06-30T23:59:59Z` | Applied/current at initial question |
| `DV-SYN-POL-VEL-V2` | `2.0` | `2026-06-10T12:00:00Z` | `2026-06-10T12:05:00Z` | `2026-07-01T00:00:00Z` | none | Pre-seeded candidate; not current until approved/applied and effective |
| `DV-SYN-FORM-HLP-2026-H1` | `2026-H1` | `2025-12-20T12:00:00Z` | `2025-12-20T12:05:00Z` | `2026-01-01T00:00:00Z` | `2026-06-30T23:59:59Z` | Current in baseline |
| `DV-SYN-FORM-HLP-CONFLICT` | `2026-H1-CONFLICT` | `2025-12-20T12:00:00Z` | `2025-12-20T12:05:00Z` | `2026-01-01T00:00:00Z` | `2026-06-30T23:59:59Z` | Scenario-only current substitute in F; never part of baseline |
| `DV-SYN-NOTE-001-V1` | `1` | `2026-06-10T15:30:00Z` | `2026-06-10T15:35:00Z` | `2026-06-10T15:30:00Z` | none | Current note |

Every version and evidence item will also carry its logical `source_id`, `source_type`, `source_title`, version label, relevant excerpt, section/location, synthetic marker, and a checksum generated according to Section 12. No checksum is calculated in this planning milestone.

### 6.2 Exact V1-to-V2 change

The authorization requirement does **not** change: both versions require PA. The prerequisite changes from **both Norlaxa and Bravex** in V1 to **either Norlaxa or Bravex** in V2. The duration remains 30 days, the payer/plan/medication/indication scope is unchanged, and neither version guarantees approval.

That single change is visually obvious and operationally meaningful:

- Under V1, the EHR proves only one of two prerequisites; the case is not ready under the documented policy criteria.
- Under V2, the same immutable EHR history satisfies the one-of-two prerequisite; the coordinator can assemble and submit the PA evidence.

V2 is pre-seeded as immutable candidate content so the demo does not parse arbitrary uploads. At the initial question time it is future-effective and not approved/applied, so V1 is selected. Submission and `PENDING` status do not change the current view. On `2026-07-02`, approval/application makes V2 governed knowledge; because its `effective_from` has passed, the `2026-07-03` question selects it. V1's bounded effective interval and supersession link remain queryable, and the original answer retains its V1 snapshot.

## 7. Cross-dimensional reconciliation

The guideline assertion and payer assertion use compatible patient/medication/indication scope but different decision dimensions:

| Source assertion | Decision dimension | Meaning |
|---|---|---|
| Guideline: Veluntra is supported after one preferred-therapy failure | `CLINICAL_APPROPRIATENESS` | The fictional clinical guide considers Veluntra a supported option |
| Payer V1: Veluntra requires PA and two prerequisite failures | `AUTHORIZATION_REQUIREMENT` and `PREREQUISITE_REQUIREMENT` | The plan imposes operational requirements before review |

Synapse must classify this pair as `COMPATIBLE_CONSTRAINT`, severity informational, resolution state `NOT_APPLICABLE`, because both can be true at once. It must display both statements and explain their separate authority boundaries. The guideline does not waive PA; the payer policy does not negate the guideline's clinical support.

Expected deterministic reconciliation text:

> The synthetic guideline supports Veluntra clinically after the documented Norlaxa failure. Separately, Harborlight Plus Payer Policy V1 requires prior authorization and documentation of both Norlaxa and Bravex. These sources address different questions—clinical support versus authorization requirements—so this is a compatible operational constraint, not a contradiction.

The baseline result may remain `HIGH` because all critical and important evidence is available, applicable, current for the question time, and mutually interpretable. The known fact that a prerequisite has not been completed is not missing information and does not by itself require escalation.

## 8. Genuine same-dimension conflict

Golden Scenario F replaces only the baseline formulary version with `DV-SYN-FORM-HLP-CONFLICT`; all other evidence and the as-of time remain the same.

| Normalized scope field | Payer V1 assertion | Conflict formulary assertion |
|---|---|---|
| Payer | `SYN-PAYER-NHH` | `SYN-PAYER-NHH` through plan ownership |
| Plan | `SYN-PLAN-HLP` | `SYN-PLAN-HLP` |
| Medication | `SYN-MED-VEL` | `SYN-MED-VEL` |
| Indication | `SYN-COND-LDS` | `SYN-COND-LDS` |
| As-of time | `2026-06-15T14:00:00Z` | `2026-06-15T14:00:00Z` |
| Effective interval | Includes the as-of time | Includes the as-of time |
| Decision dimension | `AUTHORIZATION_REQUIREMENT` | `AUTHORIZATION_REQUIREMENT` |
| Value | `PA_REQUIRED=true` | `PA_REQUIRED=false` |

This is `SAME_DIMENSION_DISAGREEMENT`, not cross-dimensional reconciliation: the assertions make opposite claims about the same authorization decision under the same normalized scope and time. The conflict changes the coordinator's next action, so severity is `HIGH`, resolution remains `UNRESOLVED`, confidence is `LOW`, and human escalation is mandatory. Synapse must show both excerpts and must not answer definitively whether PA is required.

Requested escalation action: a synthetic coverage-policy reviewer must determine which plan source governs authorization and record a reviewed resolution. Scenario F does not automatically select either source.

## 9. Source criticality matrix

| Input/source | Criticality | If absent or unavailable | Confidence effect | Escalation effect |
|---|---|---|---|---|
| Synthetic patient/case context | Critical | State that case applicability cannot be established; do not infer patient facts | `LOW` | Mandatory: request case context |
| Requested medication | Critical | State that the authorization target is missing | `LOW` | Mandatory: request medication |
| Payer/plan | Critical | State that applicable coverage rules cannot be selected | `LOW` | Mandatory: request payer/plan |
| Applicable payer policy | Critical | State that policy could not be verified; show only remaining evidence; do not infer policy contents | `LOW` | Mandatory: payer-policy human review |
| Formulary | Important | Continue with policy and other evidence; disclose missing formulary status | `MEDIUM` under this design | Not mandatory unless another trigger exists |
| Guideline | Important | Continue with coverage answer but do not claim clinical support | `MEDIUM` under this design | Not mandatory unless another trigger exists |
| Specialist note | Optional | Disclose absence; rely on sufficient EHR and authoritative documents | May remain `HIGH` | None from absence alone |

All five adapters are attempted for every supported canonical request. Criticality affects claim eligibility, confidence, and escalation; it does not suppress attempts or hide failures.

## 10. Golden scenarios

### A. Baseline prior authorization

- **As-of:** `2026-06-15T14:00:00Z`.
- **Inputs:** Primary case; all baseline source versions available; V1 current.
- **Expected answer:** Yes, Payer Policy V1 requires PA. V1 requires 30-day trials of both Norlaxa and Bravex. The record proves Norlaxa but not Bravex, so the documented prerequisites are not complete. The guideline separately supports Veluntra clinically after one failure.
- **Conflict:** `COMPATIBLE_CONSTRAINT`, informational; no contradiction.
- **Confidence:** `HIGH`—all critical and important evidence is available, applicable, current, and provenance-complete; the different dimensions are reconciled.
- **Escalation:** None. Next step is to verify/complete the documented V1 prerequisite or request payer review; do not claim authorization or approval.

### B. Cross-dimension reconciliation

- **As-of and evidence:** Same baseline as A.
- **Purpose:** Assert the conflict classifier and answer layout independently of the direct PA result.
- **Expected behavior:** Preserve and cite both guideline support and payer constraints, label them `COMPATIBLE_CONSTRAINT`, explain the authority boundary, and select neither as a winner.
- **Confidence:** `HIGH`.
- **Escalation:** None solely because of this compatible constraint.

### C. Missing critical payer evidence

- **As-of:** `2026-06-15T14:00:00Z`.
- **Injected condition:** Payer Policy adapter returns `SOURCE_UNAVAILABLE` with no evidence items; the other four adapters return their normal evidence.
- **Expected answer:** “The applicable payer policy could not be verified, so Synapse cannot determine whether prior authorization is required.” Show the available patient, guideline, formulary, and note evidence, but do not use the formulary to fabricate or replace the unavailable authoritative policy conclusion.
- **Confidence:** `LOW`—critical evidence is unavailable.
- **Escalation:** Mandatory to a synthetic coverage-policy reviewer, requesting the applicable current payer policy.

### D. Unsupported question

- **Question:** “What is the best cafeteria menu for this patient?”
- **Expected behavior:** Return the explicit unsupported-scope routing response because the question is outside `PRIOR_AUTHORIZATION`, `CLINICAL_GUIDANCE`, and `SPECIALIST_HISTORY`. Do not invoke broad clinical reasoning, create material domain claims, or generate citations.
- **Confidence:** Not evaluated because no answer result is composed; the UI must not invent a fourth confidence label or show a misleading clinical confidence value.
- **Escalation:** None; invite the user to ask a supported synthetic-demo question.

### E. Governed correction

- **Initial question:** A at `2026-06-15`; answer snapshot cites V1.
- **Submission:** Care coordinator selects pre-seeded V2 on `2026-06-16`; correction validates to `PENDING`; repeated pre-approval query still uses V1.
- **Approval/application:** Distinct knowledge reviewer acts on `2026-07-02`; `APPROVED` review, `APPLIED` correction, V2 evidence/assertion, V1-to-V2 supersession, current-view update, knowledge-update record, and audit events commit atomically.
- **Subsequent question:** `2026-07-03`; cites V2, still answers that PA is required, and states that the documented Norlaxa failure satisfies V2's one-of-two prerequisite.
- **History:** Reloading the initial answer still shows V1; V1 content and evidence remain immutable.
- **Confidence:** `HIGH` before and after, with rationale reflecting the current version and prerequisite state.
- **Escalation:** None when the transaction succeeds. A failed approval/application remains `PENDING`, records a failed attempt, and does not expose V2 as current.

### F. Genuine authorization conflict

- **As-of:** `2026-06-15T14:00:00Z`.
- **Injected condition:** Use the scenario-only conflict formulary version alongside current Payer Policy V1.
- **Expected answer:** Synapse cannot determine the PA requirement because two current same-scope sources disagree. Show both claims and do not choose a winner.
- **Conflict:** `SAME_DIMENSION_DISAGREEMENT`, `HIGH`, `UNRESOLVED`.
- **Confidence:** `LOW`.
- **Escalation:** Mandatory to a synthetic coverage-policy reviewer to resolve which source governs.

## 11. Expected material claims and evidence mapping

Claim IDs are stable design identities. A later answer creates distinct citation identities linking its immutable answer snapshot, supported claim, and evidence item. In this section, a *material claim* means a patient, clinical, coverage, authorization, formulary, or next-action proposition in the Supervisor's answer. Every such claim below has one or more evidence items. Retrieval failures, routing outcomes, correction state, and audit-lineage statements are system-status outputs; they are backed by immutable results/events and must never be repurposed as evidence for a healthcare claim.

### Scenario A claims

| Claim ID | Allowed material claim | Supporting evidence |
|---|---|---|
| `CLM-A-CASE` | The case has Lumen Drift Syndrome, requests Veluntra, and has active Harborlight Plus coverage at the as-of time | `EV-SYN-EHR-CONTEXT-001`, `EV-SYN-EHR-PLAN-001` |
| `CLM-A-PA` | Payer Policy V1 requires PA for Veluntra for this plan and indication | `EV-SYN-POL-V1-PA-001` |
| `CLM-A-V1-CRITERIA` | V1 requires both Norlaxa and Bravex prerequisites | `EV-SYN-POL-V1-STEP-001` |
| `CLM-A-HISTORY` | Norlaxa failure is documented and a Bravex trial is not documented | `EV-SYN-EHR-THERAPY-001`; corroboration from `EV-SYN-NOTE-HISTORY-001` is optional support |
| `CLM-A-GUIDE` | The guideline clinically supports Veluntra after one preferred failure | `EV-SYN-GUIDE-SUPPORT-001` |
| `CLM-A-FORM` | Veluntra is Tier 3 and formulary-listed subject to PA | `EV-SYN-FORM-STATUS-001` |
| `CLM-A-RECON` | Clinical support and authorization requirements are different decision dimensions | `EV-SYN-GUIDE-SCOPE-001`, `EV-SYN-POL-V1-PA-001` |

### Scenario B claims

Scenario B permits `CLM-A-GUIDE`, `CLM-A-PA`, and `CLM-A-RECON` only. Its focus is the supported classification and explanation, not a new clinical conclusion.

### Scenario C claims

| Claim ID | Allowed material claim | Supporting evidence/status |
|---|---|---|
| `STATUS-C-UNAVAILABLE` | The payer-policy adapter could not retrieve the applicable policy; therefore no PA conclusion is allowed | The persisted `SOURCE_UNAVAILABLE` retrieval result; this is a status output, not a healthcare claim, and creates no fabricated evidence item |
| `CLM-C-CASE` | The explicit case, medication, and plan context is available | `EV-SYN-EHR-CONTEXT-001`, `EV-SYN-EHR-PLAN-001` |
| `CLM-C-GUIDE` | The guideline supports the medication clinically but does not establish coverage | `EV-SYN-GUIDE-SUPPORT-001`, `EV-SYN-GUIDE-SCOPE-001` |
| `CLM-C-FORM` | The formulary reports Tier 3 with a PA restriction, but it does not substitute for the unavailable applicable payer policy | `EV-SYN-FORM-STATUS-001` plus the payer retrieval failure status |

Scenario C must not create a claim that the payer policy requires or does not require PA.

### Scenario D claims

No material clinical or coverage claim is allowed. “Unsupported by this demo” is a routing result derived from the versioned supported-intent policy, not a healthcare assertion and requires no source citation.

### Scenario E claims

| Claim ID | Allowed material claim | Supporting evidence/governance record |
|---|---|---|
| `CLM-E-V2-PA` | After effective approval/application, V2 still requires PA | `EV-SYN-POL-V2-PA-001` plus approved/applied update provenance |
| `CLM-E-V2-CRITERIA` | V2 requires either Norlaxa or Bravex for at least 30 days | `EV-SYN-POL-V2-STEP-001` |
| `CLM-E-READY` | The documented Norlaxa history satisfies V2's one-of-two prerequisite | `EV-SYN-EHR-THERAPY-001`, `EV-SYN-POL-V2-STEP-001` |

The Scenario E governance view also displays two non-healthcare status statements: while the correction is `PENDING`, V1 remains current; after application, V2 supersedes V1 while V1 and the original answer remain retained. Those statements are backed by correction, current-view, supersession, knowledge-update, and answer-snapshot records. They are not Supervisor healthcare claims and cannot support PA or clinical conclusions by themselves.

### Scenario F claims

| Claim ID | Allowed material claim | Supporting evidence |
|---|---|---|
| `CLM-F-POLICY` | Current Payer Policy V1 says PA is required | `EV-SYN-POL-V1-PA-001` |
| `CLM-F-FORM` | The scenario-only current formulary assertion says PA is not required | `EV-SYN-FORM-CONFLICT-001` |
| `CLM-F-CONFLICT` | The same-scope authorization assertions conflict and the PA answer is unresolved | Both evidence items above and the persisted conflict record |

No Scenario F claim may resolve the conflict or state a definitive PA answer.

## 12. Provenance identity design

Identities must never be overloaded. The minimum compatibility fields remain present on every retrieved evidence item, while distinct identifiers provide unambiguous lineage.

| Identity | Purpose | Example |
|---|---|---|
| Source system/category | Identifies the mock adapter/system; supplies `source_id` and `source_type` | `SRC-SYN-PAYER`, `PAYER_POLICY` |
| Source document | Stable logical record/document across versions | `DOC-SYN-POL-VEL` |
| Source document version | Immutable content version with temporal and governance metadata | `DV-SYN-POL-VEL-V1` |
| Evidence item | Immutable excerpt or structured evidence projection from one version | `EV-SYN-POL-V1-PA-001` |
| Assertion | Normalized proposition with scope, dimension, effective interval, status, and evidence links | `AST-SYN-POL-V1-PA` |
| Supported claim | Answer-eligible proposition supported by retrieved evidence for an interaction | `CLM-A-PA` |
| Citation | Link from one immutable answer snapshot and supported claim to one evidence item | `CIT-{answer_id}-CLM-A-PA-EV-SYN-POL-V1-PA-001` |

### Required metadata per version and evidence item

- immutable ID;
- `source_id` and `source_type`;
- `source_title`;
- logical document ID and immutable version ID;
- human-readable `version`;
- source-issued compatibility `timestamp`;
- UTC `recorded_at`;
- UTC `effective_from` and optional inclusive `effective_to` boundary as explicitly represented by the fixture contract;
- section/location;
- exact `relevant_excerpt` for evidence items;
- `synthetic=true` marker;
- content checksum and checksum algorithm identifier.

### Checksum strategy

Later fixture implementation should use SHA-256 over a documented canonical UTF-8 representation. A document-version checksum covers immutable source content plus identity-bearing version metadata, excluding runtime retrieval fields. An evidence checksum covers the immutable version ID, section/location, exact excerpt, and structured assertion-bearing fields. Object keys must be sorted and dates normalized to UTC ISO-8601 before hashing. The exact canonical serialization must be locked before fixtures are created; this document deliberately does not calculate checksum values.

Retrieval results, assertions, claims, and citations each receive their own IDs and link back to evidence. They do not reuse `source_id`, document IDs, or evidence IDs.

## 13. Minimal knowledge-graph semantics

The MVP needs only the following entity types:

- `CASE`
- `PATIENT`
- `CONDITION`
- `MEDICATION`
- `PAYER`
- `PLAN`
- `SOURCE_DOCUMENT`
- `SOURCE_DOCUMENT_VERSION`
- `EVIDENCE_ITEM`
- `ASSERTION`
- `FEEDBACK`
- `REVIEW`
- `ANSWER_SNAPSHOT`

Required domain relationships are limited to:

| Subject | Relationship | Object | Evidence/source |
|---|---|---|---|
| Case | `ABOUT_PATIENT` | Patient | EHR |
| Patient | `HAS_CONDITION` | Lumen Drift Syndrome | EHR |
| Patient | `HAS_PLAN` | Harborlight Plus | EHR |
| Patient | `HAS_PRIOR_THERAPY` | Norlaxa | EHR, optionally corroborated by note |
| Case | `REQUESTS_MEDICATION` | Veluntra | EHR, specialist note |
| Plan | `OFFERED_BY` | Northstar Harbor Health | EHR/plan metadata |
| Guideline version | `SUPPORTS_CLINICALLY` | Veluntra for Lumen Drift Syndrome | Guideline evidence |
| Policy version | `REQUIRES_AUTHORIZATION` | Veluntra for normalized plan/indication scope | Policy evidence |
| Policy version | `REQUIRES_PREREQUISITE` | Norlaxa/Bravex rule | Policy evidence |
| Formulary version | `LISTS_MEDICATION` | Veluntra at Tier 3 | Formulary evidence |
| Assertion | `SUPPORTED_BY` | Evidence item | Provenance link |
| Document version V2 | `SUPERSEDES` | Document version V1 | Approved knowledge update |
| Assertion V2 | `SUPERSEDES` | Assertion V1 | Approved knowledge update |
| Feedback | `PROPOSES` | V2 assertion | Feedback record |
| Answer snapshot | `CITES` | Evidence item | Citation record |

Authorization status, prerequisite count, tier, dates, and outcomes should be evidence-backed assertions with normalized fields rather than new ontology types. Multiple assertions may coexist; only the governed current/effective selector determines which applied assertion is current for a question. No graph database or broad healthcare ontology is required.

## 14. Temporal and effective-date design

This section preserves the original scenario intent and records the implemented v1.4 temporal behavior. The earlier v1.3 release selected `is_current` and used `as_of` only during reasoning; v1.4 now applies `as_of` during governed source and assertion selection as defined in `product-spec.md`, `architecture.md`, and `technical-architecture.md`.

- All stored times use UTC ISO-8601.
- `timestamp` is the source-issued time retained for minimum provenance compatibility.
- `recorded_at` is when Synapse recorded the immutable object.
- `effective_from` and `effective_to` define when a document version or assertion applies.
- Each explicit AS_OF scenario has an immutable `as_of` applicability time. Implemented v1.4 selection combines normalized scope, governance eligibility, and effective-interval containment; it does not use supersession lineage as a precedence rule.
- Effective intervals are closed UTC intervals. A null `effective_to` is open-ended.
- V1 applies at the initial `2026-06-15` question and ends before V2's `2026-07-01` effective start; the policy intervals do not overlap.
- V2's source issue and record dates may precede its effective date. It remains governance-ineligible for AS_OF until approved/applied. Approval currently makes V2 current immediately even if its `effective_from` is in the future; AS_OF still honors the interval, and no distinct approved-but-not-yet-current state exists.
- The `2026-07-02` approval occurs after V2 becomes effective, so the next question can select it immediately without retroactively changing the `2026-06-15` snapshot.
- The scenario-only formulary conflict variant shares the baseline version's interval only inside isolated Scenario F; it never coexists as current in the baseline seed state.

## 15. Expected confidence and escalation outcomes

The policy version used by golden expectations should be recorded as `CONF-PA-SYN-V1`. It is categorical and deterministic, not a score or clinically validated probability.

| Scenario/state | Availability | Relevance | Agreement/conflict | Freshness | Required context | Result | Escalation |
|---|---|---|---|---|---|---|---|
| A baseline | All five available | Exact scope | Compatible cross-dimension constraint | Current as of question | Complete | `HIGH` | No |
| B reconciliation | All five available | Exact scope | `COMPATIBLE_CONSTRAINT` | Current | Complete | `HIGH` | No |
| C payer unavailable | Critical policy missing | Remaining evidence relevant | Authorization cannot be verified | Policy unknown | Critical gap | `LOW` | Mandatory |
| D unsupported | Not applicable | Outside supported intents | Not evaluated | Not evaluated | Not applicable | No answer/confidence result | No |
| E pending | All baseline evidence available | Exact scope | V2 excluded by governance; V1 selected | V1 current at initial time | Complete | `HIGH` | No |
| E applied/effective | All five available | Exact scope | V2 selected; compatible constraint remains | V2 current | Complete | `HIGH` | No |
| F true conflict | All five available | Same exact scope | Unresolved high-severity same-dimension disagreement | Both current in isolated scenario | Complete but contradictory | `LOW` | Mandatory |
| Formulary missing only | Four sources available | Remaining evidence relevant | No new contradiction | Other sources current | Critical context complete | `MEDIUM` | No, absent another trigger |
| Guideline missing only | Four sources available | Coverage evidence relevant | Clinical support not established | Other sources current | Critical context complete | `MEDIUM` | No, absent another trigger |
| Specialist note missing only | Four authoritative/important sources available | Exact scope | No new contradiction | Current | Complete | `HIGH` permitted | No |

Every `LOW` result mandates escalation. Any independently missing required input or unresolved high-severity conflict also mandates escalation even if a presentation defect would otherwise omit the confidence label.

## 16. Three-minute demo narrative

1. **Orient (20 seconds):** Show the synthetic banner and case card: fictional Lumen Drift Syndrome, fictional Veluntra, fictional Harborlight Plus, and one documented Norlaxa failure.
2. **Ask (30 seconds):** Run the canonical question. Point out that all five mocked source adapters were attempted in the approved sequence.
3. **Reconcile (40 seconds):** Show `HIGH` confidence, the guideline's clinical support, Payer Policy V1's PA/two-agent rule, the compatible-constraint label, and two expandable citations. Explain that the system does not confuse clinical appropriateness with coverage requirements.
4. **Correct (35 seconds):** As the synthetic care coordinator, flag V1 and select pre-seeded V2. Show `PENDING` and rerun or inspect current state to prove V1 has not changed.
5. **Govern (35 seconds):** As the distinct synthetic knowledge reviewer at the fixed later demo time, approve/apply V2. Show the atomic update and V1-to-V2 lineage.
6. **Reuse and audit (20 seconds):** Ask again and show that PA remains required but the one-agent prerequisite is met. Reopen the first answer to show its V1 snapshot is unchanged.

Scenario C and Scenario F should be separate one-click golden cases for judges who want to see failure behavior; they should not require editing fixtures during the demo.

## 17. Open decisions before fixture implementation

These decisions require explicit approval before fixture implementation but do not reopen the approved architecture:

1. **Fixture serialization and checksum canonicalization:** choose the version-controlled fixture format and lock the exact canonical serialization used for SHA-256.
2. **Future-effective approval behavior:** decide whether a future release should reject approval before `effective_from` or represent approved-but-not-current knowledge until activation. V1.4 currently makes the approved version current immediately while AS_OF continues to honor effective intervals.
3. **Unsupported-response presentation:** confirm that an unsupported routing outcome shows no confidence badge, rather than displaying a misleading `LOW`; no fourth confidence label is permitted.
4. **Prototype disclaimer wording:** approve the final visible wording based on “SYNTHETIC — DEMO ONLY. Not for clinical or coverage decisions.”
5. **Conflict variant packaging:** confirm that the scenario-only formulary version is isolated as golden-test input and never loaded into the normal baseline current view.
6. **Next-step wording under V1:** approve concise language that requests verification or payer review without suggesting that a fictional medication should actually be started or tried.

No technology, database schema, dependency, clinical rule, or real-world integration decision is made by this design.

## 18. Validation checklist

- **Dates:** The EHR request and note precede the initial question. V1 is effective at the initial question. V2 is issued and recorded before its future effective date, approved after becoming effective, and selected only by the later question. There is no V1/V2 effective overlap.
- **V1/V2 governance:** V2 candidate and pending states are noncurrent. Approval and application are atomic. V1 remains immutable and the original answer retains its V1 snapshot.
- **Claim coverage:** Every allowed material claim in A–F has evidence or, for retrieval/governance state claims, an immutable persisted status/event. Prohibited claims are explicitly identified for missing-source and conflict cases.
- **Cross-dimensional classification:** Guideline clinical support versus payer authorization/prerequisites is `COMPATIBLE_CONSTRAINT`, not a contradiction.
- **True conflict scope:** Scenario F matches payer, plan, medication, indication, effective/as-of time, and `AUTHORIZATION_REQUIREMENT`, while asserting opposite values.
- **Synthetic safety:** Every entity and excerpt is invented and visibly synthetic; no real person, insurer, medication, policy, identifier, or clinical claim is represented.
- **Determinism:** Structured tags, fixed IDs, fixed timestamps, exact excerpts, and explicit expected outcomes allow all scenarios to run without an LLM, FTS5, or network access.
- **Implementation scope:** This milestone changes only this design document; no backend, frontend, test, database, dependency, or fixture change is part of the plan.
