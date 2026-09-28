package scopt

abstract class OptionParser[C](programName: String) {
  def opt[A](name: String): OptionDef[A] = new OptionDef[A](name)
  def parse(args: Seq[String], init: C): Option[C] = Some(init)
}

class OptionDef[A](val name: String) {
  def text(x: String): OptionDef[A] = this
}
