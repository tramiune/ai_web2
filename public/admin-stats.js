/** Admin stats — cost table & aggregation (nhay.cloud). */

export const TURBO_MODEL_IDS = new Set(['117']);
export const FAST_MODEL_IDS = new Set(['124', '125']);

export const RENDER_COST_VND = {
    xiaoyang: { fast: 400, turbo: 1000 },
    aidancing: { fast: 4500, turbo: 10000 },
    videoaieasy: { fast: 400, turbo: 1000 },
};

export const TOPUP_SUCCESS_STATUSES = new Set(['approved', 'completed']);

export function orderRenderProvider(order) {
    const rp = (order?.renderProvider || '').toLowerCase();
    if (rp === 'xiaoyang' || rp === 'aidancing' || rp === 'videoaieasy') return rp;
    if (order?.videoaieasyJobId) return 'videoaieasy';
    if (order?.xiaoyangTaskId) return 'xiaoyang';
    return 'aidancing';
}

export function orderModelTier(modelId) {
    const id = String(modelId || '').trim();
    return TURBO_MODEL_IDS.has(id) ? 'turbo' : 'fast';
}

export function orderHadRenderSubmit(order) {
    if (!order) return false;
    if (order.xiaoyangTaskId || order.aidancingJobId || order.videoaieasyJobId) return true;
    const st = order.status;
    return st === 'processing' || st === 'completed' || st === 'failed';
}

export function orderRenderCostVnd(order) {
    if (!order || order.status === 'pending') return 0;
    if (!orderHadRenderSubmit(order)) return 0;
    if (order.status !== 'completed' && order.status !== 'failed') return 0;
    const provider = orderRenderProvider(order);
    const tier = orderModelTier(order.modelId);
    const table = RENDER_COST_VND[provider];
    if (!table) return 0;
    return table[tier] ?? table.fast;
}

export function toJsDate(value) {
    if (!value) return null;
    if (value instanceof Date) return value;
    if (typeof value.toDate === 'function') return value.toDate();
    if (typeof value.seconds === 'number') return new Date(value.seconds * 1000);
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? null : d;
}

export function vnDateStringFromValue(value) {
    const d = toJsDate(value);
    if (!d) return null;
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Ho_Chi_Minh' }).format(d);
}

function emptyBucket(date) {
    return {
        date,
        topupUsers: new Set(),
        topupCount: 0,
        topupVnd: 0,
        ordersCompleted: 0,
        ordersFailed: 0,
        ordersProcessing: 0,
        ordersPending: 0,
        promoOrders: 0,
        revenueCoins: 0,
        renderCostVnd: 0,
        byEngine: {
            xiaoyang: { fast: 0, turbo: 0 },
            aidancing: { fast: 0, turbo: 0 },
            videoaieasy: { fast: 0, turbo: 0 },
        },
    };
}

function finalizeBucket(b) {
    return {
        date: b.date,
        topupUsers: b.topupUsers.size,
        topupCount: b.topupCount,
        topupVnd: b.topupVnd,
        ordersCompleted: b.ordersCompleted,
        ordersFailed: b.ordersFailed,
        ordersProcessing: b.ordersProcessing,
        ordersPending: b.ordersPending,
        promoOrders: b.promoOrders,
        revenueCoins: b.revenueCoins,
        renderCostVnd: b.renderCostVnd,
        byEngine: b.byEngine,
    };
}

export function aggregateStats(orders = [], topups = [], range) {
    const from = range.from;
    const to = range.to;
    const buckets = new Map();

    function bucketFor(dateStr) {
        if (!dateStr || dateStr < from || dateStr > to) return null;
        if (!buckets.has(dateStr)) buckets.set(dateStr, emptyBucket(dateStr));
        return buckets.get(dateStr);
    }

    const totals = emptyBucket('total');
    totals.topupUsers = new Set();

    for (const t of topups) {
        if (!TOPUP_SUCCESS_STATUSES.has(t.status)) continue;
        const dateStr = vnDateStringFromValue(t.approvedAt || t.updatedAt || t.createdAt);
        const b = bucketFor(dateStr);
        if (!b) continue;
        b.topupCount += 1;
        b.topupVnd += Number(t.amount) || 0;
        if (t.userId) {
            b.topupUsers.add(t.userId);
            totals.topupUsers.add(t.userId);
        }
        totals.topupCount += 1;
        totals.topupVnd += Number(t.amount) || 0;
    }

    for (const o of orders) {
        const dateStr = vnDateStringFromValue(o.createdAt);
        const b = bucketFor(dateStr);
        if (!b) continue;

        const st = o.status;
        if (st === 'completed') {
            b.ordersCompleted += 1;
            totals.ordersCompleted += 1;
        } else if (st === 'failed') {
            b.ordersFailed += 1;
            totals.ordersFailed += 1;
        } else if (st === 'processing') {
            b.ordersProcessing += 1;
            totals.ordersProcessing += 1;
        } else if (st === 'pending') {
            b.ordersPending += 1;
            totals.ordersPending += 1;
        }

        if (st === 'completed') {
            b.revenueCoins += Number(o.costCoins) || 0;
            totals.revenueCoins += Number(o.costCoins) || 0;
        }
        if (o.dailyPromo) {
            b.promoOrders += 1;
            totals.promoOrders += 1;
        }

        const cost = orderRenderCostVnd(o);
        if (cost > 0) {
            b.renderCostVnd += cost;
            totals.renderCostVnd += cost;
            const provider = orderRenderProvider(o);
            const tier = orderModelTier(o.modelId);
            if (b.byEngine[provider]) {
                b.byEngine[provider][tier] += 1;
                totals.byEngine[provider][tier] += 1;
            }
        }
    }

    const daily = Array.from(buckets.values())
        .sort((a, b) => a.date.localeCompare(b.date))
        .map(finalizeBucket);

    return {
        from,
        to,
        daily,
        totals: finalizeBucket(totals),
        ordersScanned: orders.length,
        topupsScanned: topups.length,
    };
}

export function formatVnd(n) {
    return `${Math.round(Number(n) || 0).toLocaleString('vi-VN')}đ`;
}

export function formatNumber(n) {
    return Math.round(Number(n) || 0).toLocaleString('vi-VN');
}
