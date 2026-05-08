import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { EyeOutlined, FolderOpenOutlined, UploadOutlined } from '@ant-design/icons'
import { Alert, Button, Form, Input, message, Modal, Select, Space, Table, Tag, Upload } from 'antd'
import { useNavigate } from 'react-router-dom'

import { fetchRecordings } from '@/api/recordings'
import {
  batchImportTranscripts,
  fetchTranscripts,
  type Transcript,
  type TranscriptBatchImportResult,
  uploadManualTranscript,
} from '@/api/transcripts'
import { formatRecordingDisplayName } from '@/utils/recording-display'
import { formatBeijingTime } from '@/utils/time'

function extractNativeFile(input: unknown): File | null {
  const candidate =
    (input as { file?: { originFileObj?: unknown } })?.file?.originFileObj ??
    (input as { originFileObj?: unknown })?.originFileObj ??
    (input as { fileList?: Array<{ originFileObj?: unknown }> })?.fileList?.[0]?.originFileObj ??
    input

  return candidate instanceof File ? candidate : null
}

export function TranscriptsPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [batchOpen, setBatchOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [lastBatchResult, setLastBatchResult] = useState<TranscriptBatchImportResult | null>(null)
  const [uploadForm] = Form.useForm()
  const [batchForm] = Form.useForm()

  const { data, isLoading } = useQuery({
    queryKey: ['transcripts', page, pageSize],
    queryFn: () => fetchTranscripts({ page, page_size: pageSize }),
  })

  const { data: recordingsData } = useQuery({
    queryKey: ['recordings', 'upload-options'],
    queryFn: () => fetchRecordings({ page_size: 200 }),
  })

  const uploadMut = useMutation({
    mutationFn: ({ file, recordingId, provider }: { file: File; recordingId: string; provider: string }) =>
      uploadManualTranscript(file, recordingId, provider),
    onSuccess: (transcript) => {
      message.success('转写文本已上传并替换当前录音的转写内容')
      setUploadOpen(false)
      uploadForm.resetFields()
      qc.invalidateQueries({ queryKey: ['transcripts'] })
      qc.invalidateQueries({ queryKey: ['recordings'] })
      navigate(`/admin/transcripts/${transcript.id}`)
    },
    onError: (error) => {
      const reason = error instanceof Error ? error.message : ''
      message.error(reason ? `上传失败：${reason}` : '上传失败')
    },
  })

  const batchMut = useMutation({
    mutationFn: ({ directory, provider }: { directory: string; provider: string }) =>
      batchImportTranscripts(directory, provider),
    onSuccess: (result) => {
      setLastBatchResult(result)
      setBatchOpen(false)
      batchForm.resetFields()
      qc.invalidateQueries({ queryKey: ['transcripts'] })
      qc.invalidateQueries({ queryKey: ['recordings'] })
      message.success(
        `批量导入完成：新增 ${result.imported}，跳过 ${result.skipped}，冲突 ${result.conflicts}，错误 ${result.errors}`,
      )
    },
    onError: (error) => {
      const reason = error instanceof Error ? error.message : ''
      message.error(reason ? `批量导入失败：${reason}` : '批量导入失败')
    },
  })

  const handleUpload = async () => {
    const values = await uploadForm.validateFields()
    const file = extractNativeFile(values.file)
    if (!file) {
      message.error('请选择有效的转写文件')
      return
    }

    setUploading(true)
    try {
      await uploadMut.mutateAsync({
        file,
        recordingId: values.recording_id,
        provider: values.provider || 'manual',
      })
    } finally {
      setUploading(false)
    }
  }

  const handleBatchImport = async () => {
    const values = await batchForm.validateFields()
    await batchMut.mutateAsync({
      directory: values.directory,
      provider: values.provider || 'validated-batch',
    })
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16, gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h2 style={{ margin: 0 }}>对话逐字稿</h2>
          <div style={{ color: '#666', marginTop: 4 }}>
            按录音查看全文转写，支持单文件上传，也支持按目录批量导入第三方 ASR 的 `payload.jsonl`
          </div>
        </div>
        <Space wrap>
          <Button icon={<FolderOpenOutlined />} onClick={() => setBatchOpen(true)}>
            目录批量导入
          </Button>
          <Button type="primary" icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>
            上传单个转写
          </Button>
        </Space>
      </div>

      {lastBatchResult && (
        <Alert
          style={{ marginBottom: 16 }}
          type={lastBatchResult.errors || lastBatchResult.conflicts ? 'warning' : 'success'}
          showIcon
          message={`最近一次批量导入：新增 ${lastBatchResult.imported}，跳过 ${lastBatchResult.skipped}，冲突 ${lastBatchResult.conflicts}，错误 ${lastBatchResult.errors}`}
          description={
            lastBatchResult.items.length ? (
              <div style={{ maxHeight: 180, overflow: 'auto', marginTop: 8 }}>
                {lastBatchResult.items.slice(0, 8).map((item) => (
                  <div key={`${item.source_path}-${item.status}`} style={{ marginBottom: 6 }}>
                    <Tag
                      color={
                        item.status === 'imported'
                          ? 'success'
                          : item.status === 'skipped'
                            ? 'default'
                            : item.status === 'conflict'
                              ? 'warning'
                              : 'error'
                      }
                    >
                      {item.status}
                    </Tag>
                    {item.recording_file_name ? `${formatRecordingDisplayName(item.recording_file_name)} - ` : ''}
                    {item.message}
                  </div>
                ))}
              </div>
            ) : null
          }
        />
      )}

      <Table
        rowKey="id"
        loading={isLoading}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (total) => `共 ${total} 条`,
          onChange: (nextPage, nextPageSize) => {
            setPage(nextPage)
            setPageSize(nextPageSize)
          },
        }}
        columns={[
          {
            title: '录音文件',
            dataIndex: 'recording_file_name',
            ellipsis: true,
            render: (value: string | null, row: Transcript) =>
              value ? formatRecordingDisplayName(value, row.created_at) : row.recording_id,
          },
          {
            title: '来源',
            dataIndex: 'asr_provider',
            width: 140,
            render: (value: string) => <Tag color={value.includes('batch') ? 'purple' : value === 'manual' ? 'gold' : 'blue'}>{value}</Tag>,
          },
          {
            title: '文本预览',
            dataIndex: 'full_text',
            ellipsis: true,
            render: (value: string | null) => value || <span style={{ color: '#999' }}>暂无文本</span>,
          },
          {
            title: '完成时间',
            dataIndex: 'completed_at',
            width: 180,
            render: (value: string | null, row: Transcript) => formatBeijingTime(value || row.created_at, 'YYYY-MM-DD HH:mm'),
          },
          {
            title: '操作',
            width: 100,
            render: (_: unknown, row: Transcript) => (
              <Button type="link" size="small" icon={<EyeOutlined />} onClick={() => navigate(`/admin/transcripts/${row.id}`)}>
                查看
              </Button>
            ),
          },
        ]}
      />

      <Modal
        title="上传单个转写"
        open={uploadOpen}
        onOk={handleUpload}
        onCancel={() => setUploadOpen(false)}
        confirmLoading={uploading || uploadMut.isPending}
        destroyOnClose
        width={560}
      >
        <Form form={uploadForm} layout="vertical" preserve={false}>
          <Form.Item
            name="recording_id"
            label="关联录音"
            rules={[{ required: true, message: '请选择录音' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder="选择需要替换转写的录音"
              options={(recordingsData?.items ?? []).map((recording) => ({
                value: recording.id,
                label: `${formatRecordingDisplayName(recording.file_name, recording.created_at)} (${formatBeijingTime(recording.created_at, 'MM/DD HH:mm')})`,
              }))}
            />
          </Form.Item>
          <Form.Item name="provider" label="来源标识" initialValue="manual">
            <Input placeholder="例如 manual / aliyun / tencent / third-party" />
          </Form.Item>
          <Form.Item
            name="file"
            label="转写文件"
            rules={[{ required: true, message: '请选择转写文件' }]}
            getValueFromEvent={(event) => event}
            extra="支持 .txt、.json、.jsonl。上传后会覆盖当前录音的转写内容，并重建片段与分析结果。"
          >
            <Upload beforeUpload={() => false} maxCount={1} accept=".txt,.json,.jsonl">
              <Button icon={<UploadOutlined />}>选择转写文件</Button>
            </Upload>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="目录批量导入"
        open={batchOpen}
        onOk={handleBatchImport}
        onCancel={() => setBatchOpen(false)}
        confirmLoading={batchMut.isPending}
        destroyOnClose
        width={620}
      >
        <Form form={batchForm} layout="vertical" preserve={false} initialValues={{ provider: 'validated-batch' }}>
          <Form.Item
            name="directory"
            label="目录路径"
            rules={[{ required: true, message: '请输入目录路径' }]}
            extra="例如 d:\\pyspace\\Agent\\validated。系统会优先扫描子目录中的 payload.jsonl。"
          >
            <Input placeholder="输入要批量导入的绝对路径" />
          </Form.Item>
          <Form.Item
            name="provider"
            label="来源标识"
            extra="同一份源文件会按内容指纹去重；同名录音如果已经有不同转写，会标记为冲突而不是直接覆盖。"
          >
            <Input placeholder="例如 validated-batch" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default TranscriptsPage
