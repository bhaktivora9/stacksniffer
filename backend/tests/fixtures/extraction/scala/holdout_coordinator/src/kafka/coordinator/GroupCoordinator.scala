package kafka.coordinator

import java.util.concurrent.atomic.AtomicBoolean
import scala.collection.{immutable, mutable => m}

sealed trait GroupState { def name: String }
case object Empty extends GroupState { val name = "Empty" }
case object Stable extends GroupState { val name = "Stable" }

class GroupCoordinator(brokerId: Int, metadata: GroupMetadataManager) {
  private val isActive = new AtomicBoolean(false)
  private val pending: m.Map[String, Int] = m.Map.empty

  def startup(enable: Boolean = true): Unit = {
    isActive.set(true)
    metadata.load(brokerId)
  }

  def handleJoin(groupId: String, memberId: String): JoinResult = {
    val group = metadata.getGroup(groupId)
    if (!isActive.get()) JoinResult.error("inactive")
    else doJoin(group, memberId)
  }

  private def doJoin(group: GroupMetadata, memberId: String): JoinResult = {
    group.add(memberId)
    group.transitionTo(Stable)
    JoinResult(memberId, group.size())
  }

  def shutdown(): Unit = isActive.set(false)
}

case class JoinResult(memberId: String, generation: Int)

object JoinResult {
  def error(reason: String): JoinResult = JoinResult(reason, -1)
}

class GroupMetadataManager {
  private val groups = m.Map.empty[String, GroupMetadata]
  def load(brokerId: Int): Unit = groups.clear()
  def getGroup(groupId: String): GroupMetadata = groups.getOrElseUpdate(groupId, new GroupMetadata(groupId))
}
