package com.acme.order.infra;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertTimeoutPreemptively;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C021. HttpPaymentGateway.create built a RestClient with no connect/read
 * timeout, so when the payment provider hung every request thread blocked indefinitely and Tomcat's
 * pool was exhausted. A hanging provider must make query() give up well within 10s (throwing, or
 * returning a degraded status, both acceptable); a healthy provider must still be passed through.
 */
class HttpPaymentGatewayTimeoutHiddenTest {

    private HttpServer server;
    private ExecutorService pool;

    @BeforeEach
    void start() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        pool = Executors.newCachedThreadPool(r -> {
            Thread t = new Thread(r, "fake-provider");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(pool);
        server.createContext("/v1/payments/P-OK", ex -> {
            byte[] body = "{\"paymentId\":\"P-OK\",\"status\":\"SUCCESS\"}".getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().add("Content-Type", "application/json");
            ex.sendResponseHeaders(200, body.length);
            ex.getResponseBody().write(body);
            ex.close();
        });
        server.createContext("/v1/payments/P-HANG", ex -> {
            try {
                Thread.sleep(30_000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            ex.close();
        });
        server.start();
    }

    @AfterEach
    void stop() {
        server.stop(0);
        pool.shutdownNow();
    }

    private HttpPaymentGateway gateway() {
        return HttpPaymentGateway.create("http://127.0.0.1:" + server.getAddress().getPort());
    }

    @Test
    void hangingProviderDoesNotBlockCallerForever() {
        var gateway = gateway();
        assertTimeoutPreemptively(Duration.ofSeconds(10), () -> {
            try {
                PaymentGateway.PaymentStatus s = gateway.query("P-HANG");
                // 降级返回也可以，但不能谎报支付成功
                assertThat(s == null ? null : s.status()).isNotEqualTo("SUCCESS");
            } catch (RuntimeException expected) {
                // 超时以异常形式抛出
            }
        });
    }

    @Test
    void healthyProviderStillPassesThrough() {
        assertThat(gateway().query("P-OK")).isEqualTo(new PaymentGateway.PaymentStatus("P-OK", "SUCCESS"));
    }
}
