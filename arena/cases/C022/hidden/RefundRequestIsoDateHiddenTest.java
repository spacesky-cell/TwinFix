package com.acme.order.refund;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.acme.order.domain.BizException;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.ZoneOffset;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C022. RefundRequest.receivedOn was pinned to @JsonFormat "yyyy/MM/dd",
 * while the customer-service console (and every other date in the API) sends ISO-8601
 * "2026-10-15", so every refund request failed to deserialize and returned 400. ISO dates must be
 * accepted and parsed to the right day; the return-window check must still use that date.
 */
class RefundRequestIsoDateHiddenTest {

    private final ObjectMapper json = new ObjectMapper().findAndRegisterModules();
    private final RefundPolicy policy = new RefundPolicy(
            Clock.fixed(Instant.parse("2026-10-20T02:00:00Z"), ZoneOffset.UTC), ZoneId.of("Asia/Shanghai"));

    private RefundRequest parse(String receivedOn) throws Exception {
        return json.readValue("""
                {"orderId": 1042, "amount": 129.50, "reason": "尺码不合适", "receivedOn": "%s"}
                """.formatted(receivedOn), RefundRequest.class);
    }

    @Test
    void acceptsIsoDateFromConsole() throws Exception {
        RefundRequest req = parse("2026-10-15");
        assertThat(req.receivedOn()).isEqualTo(LocalDate.of(2026, 10, 15));
        assertThat(req.orderId()).isEqualTo(1042);
        assertThat(req.amount()).isEqualByComparingTo("129.50");
    }

    @Test
    void parsedDateStillDrivesReturnWindow() throws Exception {
        policy.check(parse("2026-10-13"));
        RefundRequest late = parse("2026-10-12");
        assertThatThrownBy(() -> policy.check(late)).isInstanceOf(BizException.class);
    }

    @Test
    void doesNotSwapMonthAndDay() throws Exception {
        assertThat(parse("2026-03-04").receivedOn()).isEqualTo(LocalDate.of(2026, 3, 4));
    }

    @Test
    void amountIsNotTouched() throws Exception {
        assertThat(parse("2026-10-15").amount()).isEqualTo(new BigDecimal("129.50"));
    }
}
