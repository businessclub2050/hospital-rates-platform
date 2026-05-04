/**
 * Cloudflare Workers handler for the per-hospital payer intelligence endpoint.
 *
 * GET /v1/hospitals/:id/payer-intel
 *
 * Returns:
 *   - Per-payer summary stats at this hospital
 *   - Peer comparison across same-state hospitals
 *   - Rate index using discount-rate ratio (negotiated / gross) — code-mix neutral
 *   - Compliance fallback for hospitals that publish only an all-payers rollup
 *
 * See ../docs/METHODOLOGY.md for the actuarial reasoning behind the rate index.
 *
 * This file shows the handler logic in isolation. The full implementation lives
 * in src/api/handlers.ts in the private API repo, alongside ~30 other endpoints
 * (cost context, quality leaderboards, code spread analysis, ASC comparisons,
 * provider lookup, ingest admin, etc.).
 */

interface Env {
	RATES_DB: D1Database;
}

type RouteHandler = (
	req: Request,
	env: Env,
	ctx: ExecutionContext,
	params: Record<string, string>,
) => Promise<Response>;

const jsonResponse = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data, null, 2), {
		status,
		headers: { 'content-type': 'application/json; charset=utf-8' },
	});

const errorResponse = (status: number, code: string, message: string): Response =>
	jsonResponse({ error: { code, message } }, status);

export const hospitalPayerIntel: RouteHandler = async (_req, env, _ctx, params) => {
	const hospital_id = params.id;

	const hosp = await env.RATES_DB.prepare(
		`SELECT hospital_id, name, system, city, state FROM hospitals WHERE hospital_id = ?`,
	).bind(hospital_id).first();
	if (!hosp) return errorResponse(404, 'NOT_FOUND', 'Unknown hospital_id');

	const state = hosp.state as string;

	// ── 1. Per-payer summary at this hospital ────────────────────────────────
	// Discount rate (negotiated/gross) is the code-mix-neutral comparison metric.
	// See docs/METHODOLOGY.md for the reasoning.
	const myRes = await env.RATES_DB.prepare(
		`SELECT r.payer_id,
		        p.canonical_name                                       AS payer_name,
		        COUNT(*)                                               AS code_count,
		        SUM(r.n)                                               AS total_rate_rows,
		        AVG(r.negotiated_avg)                                  AS avg_negotiated,
		        AVG(CASE WHEN r.gross_charge > 0
		                 THEN r.negotiated_avg / r.gross_charge END)   AS avg_discount_rate
		 FROM rate_aggregates r
		 JOIN payers p ON p.payer_id = r.payer_id
		 WHERE r.hospital_id = ? AND r.payer_id != ''
		   AND r.negotiated_avg IS NOT NULL
		 GROUP BY r.payer_id, p.canonical_name
		 ORDER BY code_count DESC
		 LIMIT 25`,
	).bind(hospital_id).all();

	// ── 2. Peer averages across same-state hospitals ─────────────────────────
	const peerRes = await env.RATES_DB.prepare(
		`SELECT r.payer_id,
		        COUNT(DISTINCT r.hospital_id)                          AS peer_hospital_count,
		        AVG(r.negotiated_avg)                                  AS peer_avg_negotiated,
		        AVG(CASE WHEN r.gross_charge > 0
		                 THEN r.negotiated_avg / r.gross_charge END)   AS peer_avg_discount_rate
		 FROM rate_aggregates r
		 JOIN hospitals h ON h.hospital_id = r.hospital_id
		 WHERE h.state = ? AND r.hospital_id != ?
		   AND r.payer_id != '' AND r.negotiated_avg IS NOT NULL
		 GROUP BY r.payer_id`,
	).bind(state, hospital_id).all();

	type PeerRow = {
		payer_id: string;
		peer_hospital_count: number;
		peer_avg_negotiated: number;
		peer_avg_discount_rate: number | null;
	};
	const peerMap = new Map<string, PeerRow>();
	for (const row of peerRes.results ?? []) peerMap.set((row as PeerRow).payer_id, row as PeerRow);

	// ── 3. Compute rate_index per payer ──────────────────────────────────────
	// Prefer discount-rate index (code-mix neutral); fall back to raw rate ratio.
	type MyRow = {
		payer_id: string;
		payer_name: string;
		code_count: number;
		total_rate_rows: number;
		avg_negotiated: number;
		avg_discount_rate: number | null;
	};
	const payers = (myRes.results ?? []).map((row) => {
		const r = row as MyRow;
		const peer = peerMap.get(r.payer_id);

		let rate_index: number | null = null;
		let comparison_basis: 'discount_rate' | 'raw_rate' | null = null;
		if (peer) {
			if (
				r.avg_discount_rate != null &&
				peer.peer_avg_discount_rate != null &&
				peer.peer_avg_discount_rate > 0
			) {
				rate_index = Math.round((r.avg_discount_rate / peer.peer_avg_discount_rate) * 1000) / 1000;
				comparison_basis = 'discount_rate';
			} else if (peer.peer_avg_negotiated > 0) {
				rate_index = Math.round((r.avg_negotiated / peer.peer_avg_negotiated) * 1000) / 1000;
				comparison_basis = 'raw_rate';
			}
		}

		return {
			payer_id: r.payer_id,
			payer_name: r.payer_name,
			code_count: r.code_count,
			total_rate_rows: r.total_rate_rows,
			avg_negotiated: Math.round(r.avg_negotiated * 100) / 100,
			peer_avg_negotiated: peer ? Math.round(peer.peer_avg_negotiated * 100) / 100 : null,
			peer_hospital_count: peer ? peer.peer_hospital_count : 0,
			rate_index,
			comparison_basis,
			rate_index_pct: rate_index != null ? Math.round((rate_index - 1) * 100) : null,
		};
	});

	// ── 4. Headline numbers ──────────────────────────────────────────────────
	const totalRateRows = payers.reduce((s, p) => s + (p.total_rate_rows ?? 0), 0);
	const payersWithPeerData = payers.filter((p) => p.rate_index != null);
	const avgRateIndex =
		payersWithPeerData.length > 0
			? Math.round(
					(payersWithPeerData.reduce((s, p) => s + p.rate_index!, 0) / payersWithPeerData.length) *
						1000,
				) / 1000
			: null;

	// ── 5. Compliance fallback ───────────────────────────────────────────────
	// Hospitals filing only the all-payers rollup get a summary card so the
	// frontend can render the §180.50 compliance warning. See docs/COMPLIANCE.md.
	const rollupRow = (await env.RATES_DB.prepare(
		`SELECT COUNT(*) as code_count,
		        AVG(negotiated_avg) as avg_negotiated,
		        AVG(gross_charge)   as avg_gross
		 FROM rate_aggregates WHERE hospital_id = ? AND payer_id = ''`,
	)
		.bind(hospital_id)
		.first()) as { code_count: number; avg_negotiated: number; avg_gross: number } | null;

	return jsonResponse({
		hospital_id,
		state,
		has_per_payer_data: payers.length > 0,
		total_payers: payers.length,
		total_rate_rows: totalRateRows,
		avg_rate_index_vs_peers: avgRateIndex,
		peer_hospitals_in_state:
			peerMap.size > 0 ? Math.max(...[...peerMap.values()].map((p) => p.peer_hospital_count)) : 0,
		payers,
		rollup_summary:
			rollupRow && rollupRow.code_count > 0
				? {
						code_count: rollupRow.code_count,
						avg_negotiated: Math.round((rollupRow.avg_negotiated ?? 0) * 100) / 100,
						avg_gross: rollupRow.avg_gross
							? Math.round(rollupRow.avg_gross * 100) / 100
							: null,
					}
				: null,
	});
};
