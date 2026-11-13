package com.acme.order.web;

import com.acme.order.domain.BizException;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class ErrorHandler {

    private static final Logger log = LoggerFactory.getLogger(ErrorHandler.class);

    @ExceptionHandler(BizException.class)
    public ResponseEntity<Map<String, String>> biz(BizException e) {
        log.info("biz rejected: {} {}", e.code(), e.getMessage());
        return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("code", e.code(), "message", e.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, String>> unexpected(Exception e) {
        // 非预期异常：完整栈打到日志，供监控与 TwinFix 采集
        log.error("unhandled error", e);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of("code", "INTERNAL_ERROR", "message", String.valueOf(e.getMessage())));
    }
}
