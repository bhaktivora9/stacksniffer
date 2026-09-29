module Shop
  module Pricing
    def self.round(cents)
      cents.round
    end

    def self.discount_for(cents)
      cents > 10_000 ? round(cents * 0.1) : 0
    end
  end
end
