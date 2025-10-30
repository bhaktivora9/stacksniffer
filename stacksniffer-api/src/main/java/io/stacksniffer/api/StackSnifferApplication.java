package io.stacksniffer.api;

import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.annotation.EnableScheduling;

@Slf4j
@SpringBootApplication(scanBasePackages = "io.stacksniffer")
@EnableAsync
@EnableScheduling
public class StackSnifferApplication {

    public static void main(String[] args) {
        log.info("Starting StackSniffer Application...");
        SpringApplication.run(StackSnifferApplication.class, args);
        log.info("StackSniffer Application started successfully");
    }
}
