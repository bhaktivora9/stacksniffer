module Jobs
  class Queue
    def self.connect(name)
      new(name)
    end

    def initialize(name)
      @name = name
      @items = []
    end

    def pop = @items.shift
  end
end
