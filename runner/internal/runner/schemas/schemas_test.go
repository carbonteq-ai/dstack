package schemas

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestJobSpecStopDurationJSON(t *testing.T) {
	t.Run("bounded", func(t *testing.T) {
		var spec JobSpec
		require.NoError(t, json.Unmarshal([]byte(`{"stop_duration":37}`), &spec))
		require.NotNil(t, spec.StopDuration)
		assert.Equal(t, uint(37), *spec.StopDuration)
	})

	t.Run("immediate", func(t *testing.T) {
		var spec JobSpec
		require.NoError(t, json.Unmarshal([]byte(`{"stop_duration":0}`), &spec))
		require.NotNil(t, spec.StopDuration)
		assert.Zero(t, *spec.StopDuration)
	})

	t.Run("off", func(t *testing.T) {
		var spec JobSpec
		require.NoError(t, json.Unmarshal([]byte(`{"stop_duration":null}`), &spec))
		assert.Nil(t, spec.StopDuration)
	})
}
