package kafka.log

import kafka.utils.{CoreUtils, Logging}
import java.util.concurrent.locks.ReentrantLock

class UnifiedLog(val topic: String, config: LogConfig) extends Logging {
  private val lock = new ReentrantLock()
  private var segments: List[LogSegment] = Nil

  def append(records: Seq[String]): Long = CoreUtils.inLock(lock) {
    val segment = roll()
    segment.write(records)
    info(s"appended ${records.size} to $topic")
    segment.baseOffset
  }

  def roll(): LogSegment = {
    val segment = LogSegment.create(config.segmentBytes)
    segments = segment :: segments
    segment
  }

  def close(): Unit = CoreUtils.swallow(segments.foreach(_.close()))
}

case class LogConfig(segmentBytes: Int = 1024, retentionMs: Long = -1L)

class LogSegment(val baseOffset: Long) {
  def write(records: Seq[String]): Unit = records.foreach(println)
  def close(): Unit = ()
}

object LogSegment {
  def create(size: Int): LogSegment = new LogSegment(size.toLong)
}
