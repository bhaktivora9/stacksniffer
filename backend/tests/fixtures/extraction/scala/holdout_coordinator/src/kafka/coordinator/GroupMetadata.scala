package kafka.coordinator

import scala.collection.mutable.ListBuffer

@SerialVersionUID(2L)
class GroupMetadata(val groupId: String) extends Serializable with Ordered[GroupMetadata] {
  private val members = ListBuffer[String]()
  private var state: GroupState = Empty

  def add(memberId: String): Unit = members += memberId
  def size(): Int = members.size
  def transitionTo(target: GroupState): Unit = {
    require(canTransition(target), s"cannot move to ${target.name}")
    state = target
  }
  private def canTransition(target: GroupState): Boolean = target != state
  override def compare(that: GroupMetadata): Int = groupId.compareTo(that.groupId)
  def summary(): String = {
    val helper = new Formatter { def format(s: String): String = s.trim() }
    helper.format(groupId + ":" + size())
  }
}

trait Formatter { def format(s: String): String }
