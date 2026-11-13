package com.acme.order.domain;

/** Expected business rule violation (HTTP 409). Not a bug. */
public class BizException extends RuntimeException {

    private final String code;

    public BizException(String code, String message) {
        super(message);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
