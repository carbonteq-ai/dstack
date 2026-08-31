package executor

import (
	"archive/tar"
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"strings"
	"testing"
	"time"

	linuxuser "github.com/dstackai/dstack/runner/internal/runner/linux/user"
	"github.com/dstackai/dstack/runner/internal/runner/schemas"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestExecutor_WorkingDir_Set(t *testing.T) {
	var b bytes.Buffer
	ex := makeTestExecutor(t)
	baseDir, err := filepath.EvalSymlinks(t.TempDir())
	require.NoError(t, err)
	workingDir := path.Join(baseDir, "path/to/wd")

	ex.jobSpec.WorkingDir = &workingDir
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "pwd")
	err = ex.setJobWorkingDir(t.Context())
	require.NoError(t, err)
	require.Equal(t, workingDir, ex.jobWorkingDir)
	err = os.MkdirAll(workingDir, 0o755)
	require.NoError(t, err)

	err = ex.execJob(t.Context(), io.Writer(&b))
	assert.NoError(t, err)
	// Normalize line endings for cross-platform compatibility.
	assert.Equal(t, workingDir+"\n", strings.ReplaceAll(b.String(), "\r\n", "\n"))
}

func TestExecutor_WorkingDir_NotSet(t *testing.T) {
	var b bytes.Buffer
	ex := makeTestExecutor(t)
	cwd, err := os.Getwd()
	require.NoError(t, err)
	ex.jobSpec.WorkingDir = nil
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "pwd")
	err = ex.setJobWorkingDir(t.Context())
	require.NoError(t, err)
	require.Equal(t, cwd, ex.jobWorkingDir)

	err = ex.execJob(t.Context(), io.Writer(&b))
	assert.NoError(t, err)
	assert.Equal(t, cwd+"\n", strings.ReplaceAll(b.String(), "\r\n", "\n"))
}

func TestExecutor_HomeDir(t *testing.T) {
	var b bytes.Buffer
	ex := makeTestExecutor(t)
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "echo ~")

	err := ex.execJob(t.Context(), io.Writer(&b))
	assert.NoError(t, err)
	assert.Equal(t, ex.currentUser.HomeDir+"\n", strings.ReplaceAll(b.String(), "\r\n", "\n"))
}

func TestExecutor_SetJobUsesStopDuration(t *testing.T) {
	ex := makeTestExecutor(t)
	stopDuration := uint(37)

	ex.SetJob(schemas.SubmitBody{
		JobSpec: schemas.JobSpec{
			StopDuration: &stopDuration,
		},
	})

	assert.Equal(t, 37*time.Second, ex.killDelay)
}

func TestExecutor_SetJobLeavesStopDurationOffUnbounded(t *testing.T) {
	ex := makeTestExecutor(t)

	ex.SetJob(schemas.SubmitBody{
		JobSpec: schemas.JobSpec{
			StopDuration: nil,
		},
	})

	assert.Zero(t, ex.killDelay)
}

func TestStopImmediately(t *testing.T) {
	zero := uint(0)
	bounded := uint(37)

	assert.True(t, stopImmediately(&zero))
	assert.False(t, stopImmediately(&bounded))
	assert.False(t, stopImmediately(nil))
}

// Cancellation must reach the workload, not just the shell that launched it.
// Tasks run through an interactive shell (JobConfigurator._commands builds
// `/bin/sh -i -c "<commands>"`) and startCommand() starts that shell as a
// session leader with a controlling pty. The shell therefore enables job
// control and runs the workload in a separate foreground process group, so
// signalling the shell's pid alone never interrupts the workload.
func TestExecutor_CancelReachesJobUnderInteractiveShell(t *testing.T) {
	if testing.Short() {
		t.Skip()
	}

	ex := makeTestExecutor(t)
	stopDuration := uint(10)
	ex.jobSpec.StopDuration = &stopDuration
	ex.killDelay = 10 * time.Second

	marker := filepath.Join(t.TempDir(), "interrupted")
	// Outer interactive shell, inner workload that records its own interrupt.
	ex.jobSpec.Commands = []string{
		"/bin/sh", "-i", "-c",
		fmt.Sprintf(
			"/bin/sh -c 'trap \"echo interrupted >%s; exit 130\" INT; echo ready; while :; do sleep 0.1; done'",
			marker,
		),
	}
	makeCodeTar(t, ex)

	ctx, cancel := context.WithCancel(t.Context())
	go func() {
		time.Sleep(3 * time.Second)
		cancel()
	}()
	_ = ex.Run(ctx)

	// The workload writes the marker from its INT trap; poll briefly so the
	// assertion does not race the shell teardown.
	for range 50 {
		if _, err := os.Stat(marker); err == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	assert.FileExists(t, marker, "cancellation must interrupt the workload under the shell")
}

func TestExecutor_NonZeroExit(t *testing.T) {
	ex := makeTestExecutor(t)
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "exit 100")
	makeCodeTar(t, ex)

	err := ex.Run(t.Context())
	assert.Error(t, err)
	assert.NotEmpty(t, ex.jobStateHistory)
	exitStatus := ex.jobStateHistory[len(ex.jobStateHistory)-1].ExitStatus
	assert.NotNil(t, exitStatus)
	assert.Equal(t, 100, *exitStatus)
}

func TestExecutor_SSHCredentials(t *testing.T) {
	key := "== ssh private key =="

	var b bytes.Buffer
	ex := makeTestExecutor(t)
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "cat ~/.ssh/id_rsa")
	ex.repoCredentials = &schemas.RepoCredentials{
		CloneURL:   "ssh://git@github.com/dstackai/dstack-examples.git",
		PrivateKey: &key,
	}

	clean, err := ex.setupGitCredentials(t.Context())
	defer clean()
	require.NoError(t, err)

	err = ex.execJob(t.Context(), io.Writer(&b))
	assert.NoError(t, err)
	assert.Equal(t, key, b.String())
}

func TestExecutor_LocalRepo(t *testing.T) {
	var b bytes.Buffer
	ex := makeTestExecutor(t)
	cmd := fmt.Sprintf("cat %s/foo", *ex.jobSpec.RepoDir)
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, cmd)
	makeCodeTar(t, ex)

	err := ex.setupRepo(t.Context())
	require.NoError(t, err)

	err = ex.execJob(t.Context(), io.Writer(&b))
	assert.NoError(t, err)
	assert.Equal(t, "bar\n", strings.ReplaceAll(b.String(), "\r\n", "\n"))
}

func TestExecutor_Recover(t *testing.T) {
	ex := makeTestExecutor(t)
	ex.jobSpec.Commands = nil // cause a panic
	makeCodeTar(t, ex)

	err := ex.Run(t.Context())
	assert.ErrorContains(t, err, "recovered: ")
}

/* Long tests */

func TestExecutor_MaxDuration(t *testing.T) {
	if testing.Short() {
		t.Skip()
	}

	ex := makeTestExecutor(t)
	ex.killDelay = 500 * time.Millisecond
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "echo 1 && sleep 2 && echo 2")
	ex.jobSpec.MaxDuration = 1 // seconds
	makeCodeTar(t, ex)

	err := ex.Run(t.Context())

	// Assert the contract (the job is terminated for exceeding its max
	// duration) rather than the signal that achieved it. Cancellation is now
	// delivered to the job's process group, so the workload observes the
	// interrupt and exits instead of surviving until the hard kill.
	assert.ErrorContains(t, err, "max duration exceeded")
	history := ex.GetHistory(0)
	lastState := history.JobStates[len(history.JobStates)-1]
	assert.Equal(t, schemas.JobStateTerminated, lastState.State)
}

func TestExecutor_LogQuota(t *testing.T) {
	if testing.Short() {
		t.Skip()
	}

	ex := makeTestExecutor(t)
	ex.killDelay = 500 * time.Millisecond
	// Output >100 bytes to trigger the quota
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "for i in $(seq 1 20); do echo 'This line is long enough to exceed the quota easily'; done")
	ex.jobLogs.SetQuota(100)
	makeCodeTar(t, ex)

	err := ex.Run(t.Context())
	assert.ErrorContains(t, err, "log quota exceeded")

	// Verify the termination state was set
	history := ex.GetHistory(0)
	lastState := history.JobStates[len(history.JobStates)-1]
	assert.Equal(t, schemas.JobStateFailed, lastState.State)
}

func TestExecutor_RemoteRepo(t *testing.T) {
	if testing.Short() {
		t.Skip()
	}

	var b bytes.Buffer
	ex := makeTestExecutor(t)
	ex.jobSpec.RepoData = &schemas.RepoData{
		RepoType:        "remote",
		RepoBranch:      "main",
		RepoHash:        "2b83592e506ed6fe8e49f4eaa97c3866bc9402b1",
		RepoConfigName:  "Dstack Developer",
		RepoConfigEmail: "developer@dstack.ai",
	}
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "git rev-parse HEAD && git config user.name && git config user.email")
	err := ex.WriteRepoBlob(bytes.NewReader([]byte{})) // empty diff
	require.NoError(t, err)

	err = ex.setJobWorkingDir(t.Context())
	require.NoError(t, err)
	err = ex.setupRepo(t.Context())
	require.NoError(t, err)

	err = ex.execJob(t.Context(), io.Writer(&b))
	assert.NoError(t, err)
	expected := fmt.Sprintf("%s\n%s\n%s\n", ex.getRepoData().RepoHash, ex.getRepoData().RepoConfigName, ex.getRepoData().RepoConfigEmail)
	assert.Equal(t, expected, strings.ReplaceAll(b.String(), "\r\n", "\n"))
}

/* Helpers */

func makeTestExecutor(t *testing.T) *RunExecutor {
	t.Helper()
	baseDir, err := filepath.EvalSymlinks(t.TempDir())
	require.NoError(t, err)

	repo := filepath.Join(baseDir, "repo")
	body := schemas.SubmitBody{
		Run: schemas.Run{
			Id: "12346",
			RunSpec: schemas.RunSpec{
				RunName:  "red-turtle-1",
				RepoId:   "test-000000",
				RepoData: schemas.RepoData{RepoType: "local"},
				Configuration: schemas.Configuration{
					Type: "task",
				},
				ConfigurationPath: ".dstack.yml",
			},
		},
		JobSpec: schemas.JobSpec{
			Commands:    []string{"/bin/bash", "-c"},
			Env:         make(map[string]string),
			MaxDuration: 0, // no timeout
			WorkingDir:  &repo,
			RepoDir:     &repo,
			RepoData:    &schemas.RepoData{RepoType: "local"},
		},
		Secrets: make(map[string]string),
		RepoCredentials: &schemas.RepoCredentials{
			CloneURL: "https://github.com/dstackai/dstack-examples.git",
		},
	}

	tempDir := filepath.Join(baseDir, "temp")
	require.NoError(t, os.Mkdir(tempDir, 0o700))

	dstackDir := filepath.Join(baseDir, "dstack")
	require.NoError(t, os.Mkdir(dstackDir, 0o755))

	currentUser, err := linuxuser.FromCurrentProcess()
	require.NoError(t, err)
	homeDir := filepath.Join(baseDir, "home")
	require.NoError(t, os.Mkdir(homeDir, 0o700))
	currentUser.HomeDir = homeDir

	ex, err := NewRunExecutor(tempDir, dstackDir, *currentUser, new(sshdMock))
	require.NoError(t, err)

	ex.SetJob(body)
	require.NoError(t, ex.setJobUser(t.Context()))
	require.NoError(t, ex.setJobWorkingDir(t.Context()))

	return ex
}

func makeCodeTar(t *testing.T, ex *RunExecutor) {
	t.Helper()
	var b bytes.Buffer
	tw := tar.NewWriter(&b)

	files := []struct{ name, body string }{
		{"foo", "bar\n"},
	}

	for _, f := range files {
		hdr := &tar.Header{Name: f.name, Mode: 0o600, Size: int64(len(f.body))}
		require.NoError(t, tw.WriteHeader(hdr))
		_, err := tw.Write([]byte(f.body))
		require.NoError(t, err)
	}
	require.NoError(t, tw.Close())

	require.NoError(t, ex.WriteRepoBlob(&b))
}

func TestWriteDstackProfile(t *testing.T) {
	testCases := []string{
		"",
		"string 'with 'single' quotes",
		"multi\nline\tstring",
	}
	tmp := t.TempDir()
	path := tmp + "/dstack_profile"
	script := fmt.Sprintf(`. '%s'; printf '%%s' "$VAR"`, path)
	for _, value := range testCases {
		env := map[string]string{"VAR": value}
		writeDstackProfile(env, path)
		cmd := exec.CommandContext(t.Context(), "/bin/sh", "-c", script)
		out, err := cmd.Output()
		assert.NoError(t, err)
		assert.Equal(t, value, string(out))
	}
}

func TestExecutor_Logs(t *testing.T) {
	var b bytes.Buffer
	ex := makeTestExecutor(t)
	// Use printf to generate ANSI control codes.
	// \033[31m = red text, \033[1;32m = bold green text, \033[0m = reset
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "printf '\\033[31mRed Hello World\\033[0m\\n' && printf '\\033[1;32mBold Green Line 2\\033[0m\\n' && printf 'Line 3\\n'")

	err := ex.execJob(t.Context(), io.Writer(&b))
	assert.NoError(t, err)

	logHistory := ex.GetHistory(0).JobLogs
	assert.NotEmpty(t, logHistory)

	logString := combineLogMessages(logHistory)
	normalizedLogString := strings.ReplaceAll(logString, "\r\n", "\n")

	expectedOutput := "Red Hello World\nBold Green Line 2\nLine 3\n"
	assert.Equal(t, expectedOutput, normalizedLogString, "Should strip ANSI codes from regular logs")

	// Verify timestamps are in order
	assert.Greater(t, len(logHistory), 0)
	for i := 1; i < len(logHistory); i++ {
		assert.GreaterOrEqual(t, logHistory[i].Timestamp, logHistory[i-1].Timestamp)
	}
}

func TestExecutor_LogsWithErrors(t *testing.T) {
	var b bytes.Buffer
	ex := makeTestExecutor(t)
	ex.jobSpec.Commands = append(ex.jobSpec.Commands, "echo 'Success message' && echo 'Error message' >&2 && exit 1")

	err := ex.execJob(t.Context(), io.Writer(&b))
	assert.Error(t, err)

	logHistory := ex.GetHistory(0).JobLogs
	assert.NotEmpty(t, logHistory)

	logString := combineLogMessages(logHistory)
	normalizedLogString := strings.ReplaceAll(logString, "\r\n", "\n")

	expectedOutput := "Success message\nError message\n"
	assert.Equal(t, expectedOutput, normalizedLogString)
}

func TestExecutor_LogsAnsiCodeHandling(t *testing.T) {
	var b bytes.Buffer
	ex := makeTestExecutor(t)

	// Test a variety of ANSI escape sequences on stdout and stderr.
	cmd := "printf '\\033[31mRed\\033[0m \\033[32mGreen\\033[0m\\n' && " +
		"printf '\\033[1mBold\\033[0m \\033[4mUnderline\\033[0m\\n' && " +
		"printf '\\033[s\\033[uPlain text\\n' >&2"

	ex.jobSpec.Commands = append(ex.jobSpec.Commands, cmd)

	err := ex.execJob(t.Context(), io.Writer(&b))
	assert.NoError(t, err)

	// 1. Check WebSocket logs, which should preserve ANSI codes.
	wsLogHistory := ex.GetJobWsLogsHistory()
	assert.NotEmpty(t, wsLogHistory)
	wsLogString := combineLogMessages(wsLogHistory)
	normalizedWsLogString := strings.ReplaceAll(wsLogString, "\r\n", "\n")

	expectedWsOutput := "\033[31mRed\033[0m \033[32mGreen\033[0m\n" +
		"\033[1mBold\033[0m \033[4mUnderline\033[0m\n" +
		"\033[s\033[uPlain text\n"
	assert.Equal(t, expectedWsOutput, normalizedWsLogString, "Websocket logs should preserve ANSI codes")

	// 2. Check regular job logs, which should have ANSI codes stripped.
	regularLogHistory := ex.GetHistory(0).JobLogs
	assert.NotEmpty(t, regularLogHistory)
	regularLogString := combineLogMessages(regularLogHistory)
	normalizedRegularLogString := strings.ReplaceAll(regularLogString, "\r\n", "\n")

	expectedRegularOutput := "Red Green\n" +
		"Bold Underline\n" +
		"Plain text\n"
	assert.Equal(t, expectedRegularOutput, normalizedRegularLogString, "Regular logs should have ANSI codes stripped")

	// Verify timestamps are ordered for both log types.
	assert.Greater(t, len(wsLogHistory), 0)
	for i := 1; i < len(wsLogHistory); i++ {
		assert.GreaterOrEqual(t, wsLogHistory[i].Timestamp, wsLogHistory[i-1].Timestamp)
	}
}

type sshdMock struct{}

func (d *sshdMock) Port() int {
	return 0
}

func (d *sshdMock) Start(context.Context) error {
	return nil
}

func (d *sshdMock) Stop(context.Context) error {
	return nil
}

func (d *sshdMock) AddAuthorizedKeys(context.Context, ...string) error {
	return nil
}

func combineLogMessages(logHistory []schemas.LogEvent) string {
	var logOutput bytes.Buffer
	for _, logEvent := range logHistory {
		logOutput.Write(logEvent.Message)
	}
	return logOutput.String()
}
