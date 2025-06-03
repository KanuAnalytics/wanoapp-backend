"""
app/models/advertising.py
"""


from typing import Optional, List, Dict
from datetime import datetime
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from app.models.base import BaseDocument, PyObjectId, CampaignStatus

class AdTargeting(BaseModel):
    """Ad targeting configuration"""
    countries: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)
    categories: List[str] = Field(default_factory=list)
    age_min: Optional[int] = Field(None, ge=13, le=100)
    age_max: Optional[int] = Field(None, ge=13, le=100)
    gender: Optional[str] = Field(None, pattern="^(all|male|female|other)$")
    tribes: Optional[List[str]] = None
    
    @model_validator(mode='after')
    def validate_age_range(self):
        """Validate age range"""
        if self.age_min and self.age_max and self.age_min > self.age_max:
            raise ValueError("age_min must be less than or equal to age_max")
        return self

class AdCampaign(BaseDocument):
    """Ad campaign model"""
    advertiser_id: PyObjectId
    name: str = Field(..., min_length=1, max_length=200)
    video_id: PyObjectId
    
    # Campaign settings
    status: CampaignStatus = Field(default=CampaignStatus.DRAFT)
    start_date: datetime
    end_date: datetime
    
    # Budget and pricing
    total_budget: float = Field(..., gt=0)
    daily_budget: Optional[float] = Field(None, gt=0)
    bid_amount: float = Field(..., gt=0)
    pricing_model: str = Field(default="cpm", pattern="^(cpm|cpc|cpv)$")
    
    # Targeting
    targeting: AdTargeting
    
    # Landing page
    landing_url: Optional[HttpUrl] = None
    
    # Performance metrics
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    views: int = Field(default=0, ge=0)
    spent: float = Field(default=0.0, ge=0)
    
    # Auction settings
    auction_priority: float = Field(default=1.0, gt=0)
    frequency_cap: Optional[Dict[str, int]] = None
    
    # Renewal
    auto_renew: bool = Field(default=False)
    renewal_count: int = Field(default=0, ge=0)
    
    # Payment
    payment_method_id: Optional[str] = None
    transaction_ids: List[str] = Field(default_factory=list)
    
    @model_validator(mode='after')
    def validate_dates(self):
        """Validate campaign dates"""
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        return self
    
    @model_validator(mode='after')
    def validate_daily_budget(self):
        """Validate daily budget against total budget"""
        if self.daily_budget and self.total_budget and self.start_date and self.end_date:
            campaign_days = (self.end_date - self.start_date).days
            if self.daily_budget * campaign_days > self.total_budget:
                raise ValueError("Daily budget exceeds total budget for campaign duration")
        return self