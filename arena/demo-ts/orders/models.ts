export type CouponKind = "amount" | "percent";

export interface Coupon {
  code: string;
  kind: CouponKind;
  /** amount: 面额（分）；percent: 百分比（10 表示 10%） */
  value: number;
  /** 使用门槛（分），默认 0 */
  threshold?: number;
}

export interface OrderLine {
  sku: string;
  quantity: number;
  /** 单价（分） */
  unitPrice: number;
}

export interface PriceBreakdown {
  subtotal: number;
  discount: number;
  appliedCoupon: string | null;
  total: number;
}

export interface Order {
  id: string;
  customer: string;
  lines: OrderLine[];
  total: number;
}
