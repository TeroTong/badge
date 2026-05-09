import { api } from './client'

export type AnalysisSummary = {
  file_id: string
  recorded_at: string | null
  audio_start_time: string | null
  audio_end_time: string | null
  duration_ms: number
  duration_display: string
  segment_count: number
  overall_score: number
  eval_issue_count: number
  overall_summary: string
  dialogue_type: string
  primary_demand_summary: string | null
  focus_areas: string[]
  recommendation_count: number
  standardized_indication_count: number
  indication_names: string[]
  concern_count: number
  tag_count: number
  weight_1_tag_count: number
  consumption_intent_present: boolean
  inference_note: string | null
  analysis_version: 'new' | 'legacy'
  recording_file_name: string | null
}

export type PrimaryDemandItem = {
  priority: number
  demand: string
  body_part: string | null
  evidence: string
}

export type StaffRecommendationItem = {
  recommendation: string
  product_or_solution: string | null
  body_part: string | null
  evidence: string
  customer_response: string
  demand_priority: number[]
}

export type StandardizedIndicationItem = {
  department_code: string
  department_name: string
  indication_code: string
  indication_name: string
  body_part_code: string
  body_part_name: string
  evidence: string
}

export type AnalysisListResponse = {
  items: AnalysisSummary[]
  total: number
  page: number
  page_size: number
}

export type ConcernItem = {
  type: string
  content: string
  evidence: string
}

export type FocusArea = {
  area: string
  surface_need: string
  deep_need: string
  discovery_process: string
}

export type EvalDimension = {
  name: string
  score?: number
  point_score?: number
  max_score?: number
  comment?: string
  status?: string
  issues: { description: string; evidence: string }[]
  summary?: string
}

export type ConsultationResult = {
  chief_complaint_and_indications: {
    summary: string
    primary_demands: string[]
    standardized_indications: string[]
  }
  customer_profile_summary: {
    summary: string
    extracted_tag_count: number
    age?: string | null
    age_evidence?: string | null
    tags: { category: string; value: string; weight_level?: number; evidence?: string }[]
  }
  deal_factors: {
    summary: string
    budget: string | null
    concerns: string[]
    decision_factors: string[]
  }
  recommended_plan: {
    summary: string
    items: {
      plan: string
      acceptance: string | null
      evidence: string | null
    }[]
  }
  deal_outcome: {
    status: string
    summary: string
    deal_items: string[]
    amount: string | null
    loss_reasons: string[]
  }
}

export type ConsultationProcessEvaluationCheckpoint = {
  code: string
  name: string
  point_score?: number | null
  max_score?: number
  status?: string
  summary?: string
  evidence: string[]
  issues: { description: string; evidence: string }[]
}

export type ConsultationProcessEvaluationSection = {
  code: string
  name: string
  point_score?: number | null
  max_score?: number
  status?: string
  summary?: string
  checkpoints: ConsultationProcessEvaluationCheckpoint[]
}

export type ConsultationProcessEvaluation = {
  total_score?: number
  max_total_score?: number
  overall_score?: number
  overall_summary?: string
  sections: ConsultationProcessEvaluationSection[]
}

export type SapConsultationPreviewPayload = {
  text?: string
  user?: string
  zxxx?: Record<string, string | number | null | undefined>
  TAB_SYZ?: {
    CCKS?: string
    CCSYZ?: string
    CCBW?: string
  }[]
}

export type SapConsultationPreview = {
  recording_id?: string
  visit_order_no?: string
  visit_order_seg?: string | number | null
  customer_name?: string
  customer_code?: string
  advisor_name?: string
  indication_count?: number
  recording_count?: number
  target_count?: number
  targets?: unknown[]
  payloads?: SapConsultationPreviewPayload[]
}

export type ConsumptionIntent = {
  budget: string | null
  willingness: string
  decision_factors: string[]
  evidence: string[]
}

export type AnalysisDetail = AnalysisSummary & {
  transcript?: Record<string, unknown> | null
  customer_primary_demands: {
    inference_note: string | null
    summary: string
    items: PrimaryDemandItem[]
  } | null
  staff_recommendations: {
    summary: string
    items: StaffRecommendationItem[]
  } | null
  standardized_indications: {
    inference_note: string | null
    summary: string
    items: StandardizedIndicationItem[]
  } | null
  customer_demands: {
    inference_note: string | null
    focus_areas: FocusArea[]
    expectation: {
      dialogue_type: string
      entry_state: string
      exit_state: string
      turning_points: string[]
      specific_standards: string | null
    }
    product_preference: {
      preferred_products: string[]
      information_sources: string[]
      comparison_factors: string[]
      consultant_influence: string
    }
  }
  customer_concerns: {
    inference_note: string | null
    summary: string
    items: ConcernItem[]
  }
  customer_profile: {
    inference_note: string | null
    age?: string | null
    age_evidence?: string | null
    tags: { category: string; value: string; weight_level?: number; evidence?: string }[]
  }
  consumption_intent: {
    budget: string | null
    willingness: string
    decision_factors: string[]
    evidence: string[]
  } | null
  consultation_evaluation: {
    total_score?: number
    max_total_score?: number
    overall_score?: number
    overall_summary?: string
    dimensions: EvalDimension[]
  }
  consultation_result: ConsultationResult
  consultation_process_evaluation: ConsultationProcessEvaluation
  sap_consultation_preview?: SapConsultationPreview | null
}

export async function fetchAnalysisResults(
  sortBy = 'time',
  sortOrder = 'desc',
  page = 1,
  pageSize = 20,
  hospitalCode?: string | null,
): Promise<AnalysisListResponse> {
  const searchParams: Record<string, string | number> = {
    sort_by: sortBy,
    sort_order: sortOrder,
    page,
    page_size: pageSize,
  }
  if (hospitalCode) searchParams.hospital_code = hospitalCode
  return api.get('analysis/results', { searchParams }).json()
}

export async function fetchAnalysisDetail(fileId: string): Promise<AnalysisDetail> {
  return api.get(`analysis/results/${fileId}`).json()
}

export function extractRecordingIdFromAnalysisFileId(fileId: string): string | null {
  if (!fileId.startsWith('recording_')) return null
  const recordingId = fileId.slice('recording_'.length)
  return recordingId.length === 12 ? recordingId : null
}
