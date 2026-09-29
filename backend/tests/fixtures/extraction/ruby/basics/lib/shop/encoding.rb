if defined?(Oj)
  def encode(value) = Oj.dump(value)
else
  def encode(value) = value.to_s
end

def export(value)
  encode(value)
end
