require_relative 'queue'

module Jobs
  module Retryable
    def with_retries(attempts = 3)
      yield
    rescue StandardError
      attempts -= 1
      retry if attempts.positive?
      raise
    end
  end

  class Worker
    include Retryable

    def self.start(queue_name)
      new(Queue.connect(queue_name)).run
    end

    def initialize(queue)
      @queue = queue
    end

    def run
      while (job = @queue.pop)
        with_retries { perform(job) }
      end
    end

    def perform(job)
      job.call
    end
  end

  class LoggingWorker < Worker
    def perform(job)
      warn "performing #{job}"
      super
    end
  end
end
