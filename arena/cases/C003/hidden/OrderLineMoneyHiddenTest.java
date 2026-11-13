package com.acme.order.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.math.BigDecimal;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C003. The bug computed line totals in double, which drifts a cent
 * whenever the exact result lands on a .xx5 boundary that binary floating point cannot
 * represent. Verifies exact BigDecimal arithmetic: unitPrice * quantity * (1 - discountRate).
 */
class OrderLineMoneyHiddenTest {

    @Test
    void fortyFivePercentOn89_90() {
        // 89.90 * 0.55 = 49.445 exactly -> HALF_UP 49.45.
        OrderLine line = new OrderLine("SKU-2", 1, new BigDecimal("89.90"), new BigDecimal("0.45"));
        assertThat(line.total()).isEqualByComparingTo("49.45");
    }

    @Test
    void thirtyOnePercentOn129_50() {
        // 129.50 * 0.69 = 89.355 exactly -> 89.36; double gives 89.354999...99 -> 89.35.
        OrderLine line = new OrderLine("SKU-3", 1, new BigDecimal("129.50"), new BigDecimal("0.31"));
        assertThat(line.total()).isEqualByComparingTo("89.36");
    }

    @Test
    void threeUnitsAtFifteenPercentOn129_50() {
        // 129.50 * 3 * 0.85 = 330.225 exactly -> 330.23; double gives 330.224999...97 -> 330.22.
        OrderLine line = new OrderLine("SKU-3", 3, new BigDecimal("129.50"), new BigDecimal("0.15"));
        assertThat(line.total()).isEqualByComparingTo("330.23");
    }

    @Test
    void fiveUnitsAtThreePercentOn129_50() {
        // 129.50 * 5 * 0.97 = 628.075 exactly -> 628.08; double gives 628.074999...99 -> 628.07.
        OrderLine line = new OrderLine("SKU-3", 5, new BigDecimal("129.50"), new BigDecimal("0.03"));
        assertThat(line.total()).isEqualByComparingTo("628.08");
    }

    @Test
    void noDiscountKeepsFullPrecision() {
        OrderLine line = new OrderLine("SKU-2", 3, new BigDecimal("89.90"));
        assertThat(line.total()).isEqualByComparingTo("269.70");
    }

    @Test
    void resultIsScaleTwo() {
        OrderLine line = new OrderLine("SKU-1", 1, new BigDecimal("299.00"), new BigDecimal("0.15"));
        assertThat(line.total().scale()).isEqualTo(2);
    }

    @Test
    void hundredPercentDiscountIsZeroNotNegative() {
        OrderLine line = new OrderLine("SKU-1", 2, new BigDecimal("10.00"), BigDecimal.ONE);
        assertThat(line.total()).isEqualByComparingTo("0.00");
    }
}
