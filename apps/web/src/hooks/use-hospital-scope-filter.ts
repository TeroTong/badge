import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import * as adminApi from '@/api/admin'
import { isHospitalAdminOrAbove, isSystemAdminOrAbove } from '@/app/roles'
import { useAuth } from '@/app/use-auth'

const DEFAULT_HOSPITAL_CODE = '6501'

export function useHospitalScopeFilter() {
  const auth = useAuth()
  const user = auth.status === 'authenticated' ? auth.user : null
  const canSelectHospital = Boolean(user && isHospitalAdminOrAbove(user.role))
  const userHospitalCode = user?.hospital_code?.trim() || undefined
  const userHospitalName = user?.hospital_name?.trim() || undefined
  const [selectedHospitalCode, setSelectedHospitalCode] = useState<string | undefined>()

  const hospitalOptionsQuery = useQuery({
    queryKey: ['staff', 'hospital-options'],
    queryFn: () => adminApi.fetchStaffHospitalOptions(),
    enabled: auth.status === 'authenticated',
    staleTime: 300_000,
  })
  const hospitalOptions = useMemo(
    () => hospitalOptionsQuery.data ?? [],
    [hospitalOptionsQuery.data],
  )
  const hospitalOptionCodes = useMemo(
    () => new Set(hospitalOptions.map((item) => item.hospital_code)),
    [hospitalOptions],
  )

  const defaultHospitalCode = useMemo(() => {
    if (
      userHospitalCode
      && !isSystemAdminOrAbove(user?.role)
      && (!hospitalOptions.length || hospitalOptionCodes.has(userHospitalCode))
    ) {
      return userHospitalCode
    }
    if (hospitalOptionCodes.has(DEFAULT_HOSPITAL_CODE)) {
      return DEFAULT_HOSPITAL_CODE
    }
    return hospitalOptions[0]?.hospital_code || userHospitalCode
  }, [hospitalOptionCodes, hospitalOptions, user?.role, userHospitalCode])

  const validSelectedHospitalCode =
    selectedHospitalCode && (!hospitalOptions.length || hospitalOptionCodes.has(selectedHospitalCode))
      ? selectedHospitalCode
      : undefined
  const hospitalCode = validSelectedHospitalCode || defaultHospitalCode
  const hospitalName = hospitalOptions.find((item) => item.hospital_code === hospitalCode)?.hospital_name
    || (hospitalCode === userHospitalCode ? userHospitalName : undefined)
    || hospitalCode

  const selectOptions = hospitalOptions.map((item) => ({
    label: item.hospital_name && item.hospital_name !== item.hospital_code
      ? `${item.hospital_name} (${item.hospital_code})`
      : item.hospital_code,
    value: item.hospital_code,
  }))

  return {
    canSelectHospital,
    hospitalCode,
    hospitalName,
    hospitalOptions,
    selectOptions,
    isLoading: hospitalOptionsQuery.isLoading,
    isReady: auth.status !== 'loading' && !hospitalOptionsQuery.isLoading,
    setHospitalCode: setSelectedHospitalCode,
  }
}
