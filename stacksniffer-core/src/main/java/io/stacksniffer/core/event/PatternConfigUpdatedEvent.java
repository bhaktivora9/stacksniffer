package io.stacksniffer.core.event;

import java.time.LocalDateTime;
import java.util.List;

import io.stacksniffer.core.model.config.PatternConfig;
import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;

@Data
@Builder
@AllArgsConstructor
@NoArgsConstructor
public class PatternConfigUpdatedEvent {
	public PatternConfigUpdatedEvent(List<String> of, String string, LocalDateTime now) {
	}

	private String eventId;
	private PatternConfig config;
	private String updatedBy;
	private LocalDateTime timestamp;
	private String changeDescription;
}
