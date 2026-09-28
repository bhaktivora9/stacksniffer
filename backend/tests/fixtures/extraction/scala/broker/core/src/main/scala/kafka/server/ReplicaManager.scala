package kafka.server

import kafka.log.{LogConfig, UnifiedLog}
import kafka.utils._

class ReplicaManager(config: LogConfig) extends Logging {
  private val logs = scala.collection.mutable.Map[String, UnifiedLog]()

  def getOrCreateLog(topic: String): UnifiedLog =
    logs.getOrElseUpdate(topic, new UnifiedLog(topic, config))

  def appendRecords(topic: String, records: Seq[String]): Long = {
    val log: UnifiedLog = getOrCreateLog(topic)
    val offset = log.append(records)
    info(s"$topic -> $offset")
    offset
  }

  def shutdown(): Unit = logs.values.foreach(log => CoreUtils.swallow(log.close()))
}
